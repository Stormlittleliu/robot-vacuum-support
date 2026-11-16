# -*- coding: utf-8 -*-
"""Development tests for question lifecycle + cost routing.

These are same-family migration checks and counterexamples.  They are explicitly
NOT holdout and NOT a success rate.  No reviewed dataset, user note, frozen
artifact or holdout is read or modified here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "tests"))

from core.observability.turn_usage import TurnUsage  # noqa: E402
from robot_vacuum_support import interpretation as interp  # noqa: E402
from robot_vacuum_support import questions  # noqa: E402
from robot_vacuum_support import routing  # noqa: E402
from robot_vacuum_support import state as support  # noqa: E402

CASES_PATH = Path(__file__).with_name("cases.json")


def load_cases() -> list[dict]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert payload["split"] == "development"
    return list(payload["cases"])


def play(turns: list[str], answers: list[str] | None = None):
    state = support.empty_state()
    results = []
    for index, text in enumerate(turns, start=1):
        decision = routing.decide(state, text)
        bundle = decision.get("bundle")
        result = support.apply_user_turn(
            state,
            text,
            f"m-{index}",
            now=100 + index,
            interpretation=bundle,
            route=decision["route"],
            route_reason=decision["reason"],
        )
        state = result["state"]
        if decision["route"] == "protocol_ack":
            state = support.mark_protocol_ack(state, state.get("turn"))
        answer = result.get("direct_answer")
        if not answer and decision["route"] == "protocol_ack":
            answer = support.protocol_ack_answer(state)
        if not answer and answers and index - 1 < len(answers):
            answer = answers[index - 1]
        if not answer and decision["route"] not in {"protocol_ack"}:
            # Model-turn placeholder: a plain answer with no question.  The
            # audit therefore sees a questionless answer unless the route is a
            # direct clarification.
            answer = "已按本轮资料给一个最低成本的下一步。"
        if answer:
            state = support.apply_assistant_turn(
                state,
                answer,
                f"a-{index}",
                turn=state.get("turn"),
                now=200 + index,
            )
        results.append({"turn": index, "route": decision, "result": result})
    return state, results


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
def test_question_lifecycle_development_case(case):
    state, played = play(case["turns"])
    view = support.public_view(state)
    active = view["active_case"]
    expected = case["expect"]
    case_data = active or {}

    if expected.get("first_route"):
        assert played[0]["route"]["route"] == expected["first_route"], case["id"]
    if expected.get("route"):
        assert played[-1]["route"]["route"] == expected["route"], case["id"]
    if expected.get("route_after"):
        assert played[-1]["route"]["route"] == expected["route_after"], case["id"]
    if expected.get("answered_slot"):
        row = next(
            item for item in case_data.get("questions") or [] if item["slot"] == expected["answered_slot"]
        )
        assert row["status"] == "answered", case["id"]
        assert row["answer_value"] == expected["answered_value"], case["id"]
        assert row["resolved_turn"] is not None, case["id"]
    if expected.get("prompt_forbids_reask"):
        prompt = support.prompt_context(state)
        assert "本轮没有需要新增的澄清问题" in prompt, case["id"]
        assert "dock_find_referent=robot_cannot_return" in prompt, case["id"]
        assert "待确认问题：" not in prompt, case["id"]
    if expected.get("tried") is not None:
        assert [[item["action_id"], item["outcome"]] for item in case_data.get("tried_actions") or []] == expected["tried"], case["id"]
    if expected.get("not_tried") is not None:
        assert [item["action_id"] for item in case_data.get("not_tried_actions") or []] == expected["not_tried"], case["id"]
    if expected.get("never_answered"):
        row = next(item for item in case_data.get("questions") or [] if item["slot"] == expected["never_answered"])
        assert row["status"] != "answered", case["id"]
        assert int(row["ask_count"]) <= expected["max_ask_count"], case["id"]
        assert row["answer_value"] is None, case["id"]
    if expected.get("blocked_visible"):
        assert (view.get("decision_snapshot") or {}).get("blocked_unanswered") or any(
            int(row.get("ask_count") or 0) >= questions.MAX_ASKS for row in case_data.get("questions") or []
        ), case["id"]
    if expected.get("final_status_in"):
        row = next(item for item in case_data.get("questions") or [] if item["slot"] == "dock_find_referent")
        assert row["status"] in expected["final_status_in"], case["id"]
    if expected.get("old_answer_history"):
        row = next(item for item in case_data.get("questions") or [] if item["slot"] == "dock_find_referent")
        assert row["answer_history"][0]["answer_value"] == expected["old_answer_history"], case["id"]
        assert row.get("reopen_reason") == "user_correction_with_new_evidence", case["id"]
    if expected.get("scope_after_switch"):
        assert played[2]["result"]["scope"] == expected["scope_after_switch"], case["id"]
    if expected.get("deferred_task_topic"):
        snapshot = support.decision_snapshot(state)
        topics = [item["topic"] for item in snapshot.get("deferred_tasks") or []]
        assert expected["deferred_task_topic"] in topics, case["id"]
    if expected.get("old_task_status"):
        task = next(item for item in view["open_tasks"] if item["case_id"] == case_data["case_id"])
        assert task["status"] == expected["old_task_status"], case["id"]
    if expected.get("first_reminder_allowed"):
        # Turn 3 is the topic switch; its snapshot may allow one reminder.
        snapshot3 = support.decision_snapshot(played[2]["result"]["state"])
        assert (snapshot3.get("deferred_tasks") or [{}])[0].get("reminder_allowed") is True, case["id"]
    if expected.get("second_reminder_allowed") is False:
        snapshot4 = support.decision_snapshot(played[3]["result"]["state"])
        assert (snapshot4.get("deferred_tasks") or [{}])[0].get("reminder_allowed") is False, case["id"]
    if expected.get("status"):
        assert case_data.get("status") == expected["status"], case["id"]
    if expected.get("closure_level"):
        assert (case_data.get("closure_evidence") or {}).get("level") == expected["closure_level"], case["id"]
    if expected.get("no_model_route"):
        bundle = played[-1]["route"].get("bundle") or {}
        assert (bundle.get("trace") or {}).get("usage_status") == "not_called", case["id"]


def test_actual_asked_is_audited_from_answer_text_not_only_program_state():
    state = support.empty_state()
    first = support.apply_user_turn(state, "回充失败，找不到充电座", "m-1", now=1)
    state = support.apply_assistant_turn(
        first["state"], str(first["direct_answer"]), "a-1", turn=1, now=2
    )
    row = questions.find_question(state["cases"][0], "dock_find_referent")
    assert row["ask_count"] == 1 and row["asked_turn"] == 1
    # Model repeats the answered-referent question after the user explicitly
    # answered.  The audit must catch the answer text, not rely on state.
    second = support.apply_user_turn(
        state,
        "机器人回不去了",
        "m-2",
        now=3,
        interpretation=routing.deterministic_bundle(
            "机器人回不去了",
            method="deterministic_slot_answer",
            slot="dock_find_referent",
            value="robot_cannot_return",
            reason="unambiguous_binary_slot_answer",
        ),
        route="deterministic_slot_answer",
        route_reason="unambiguous_binary_slot_answer",
    )
    state = support.apply_assistant_turn(
        second["state"],
        "另外，您说的“找不到充电座”，我还是想确认一下：是机器人自己回不去，还是您找不到充电座放在哪？",
        "a-2",
        turn=2,
        now=4,
    )
    audit = state["last_question_audit"]
    assert audit["unexpected_asked"], audit
    assert audit["unexpected_asked"][0]["slot"] == "dock_find_referent"
    row = questions.find_question(state["cases"][0], "dock_find_referent")
    assert row["status"] == "answered"
    assert row["answer_value"] == "robot_cannot_return"


def test_prompt_and_answer_share_one_decision_snapshot():
    state = support.empty_state()
    result = support.apply_user_turn(
        state, "回充失败，找不到充电座", "m-1", now=1, route="semantic", route_reason="ambiguous"
    )
    snapshot = result["state"]["decision_snapshot"]
    prompt = support.prompt_context(result["state"], snapshot)
    assert snapshot["snapshot_id"] in prompt
    assert snapshot["current_question"]["question_id"] in prompt
    state_after = support.apply_assistant_turn(
        result["state"], str(result["direct_answer"]), "a-1", turn=1, now=2
    )
    # The audit used the same slot/question record the prompt exposed.
    audit = state_after["last_question_audit"]
    assert audit["actual_asked"][0]["question_id"] == snapshot["current_question"]["question_id"]


def test_replay_cache_requires_same_message_and_state_version():
    state = support.empty_state()
    bundle = {
        "interpretation": interp.fallback_interpretation("回充失败，找不到充电座"),
        "trace": {
            "method": "llm_structured",
            "model": "fake",
            "usage": {"total_tokens": 20},
            "fallback_used": False,
            "validation_errors": [],
        },
    }
    version = routing.state_version(state)
    assert routing.remember_interpretation(
        state, "回充失败，找不到充电座", bundle, state_version_value=version
    )
    hit = routing.decide(state, "回充失败，找不到充电座")
    assert hit["route"] == "replay_cache"
    assert (hit["bundle"]["trace"] or {}).get("usage") is None
    assert len(state["interpretation_cache"]) == 1
    # A different factual state version must not reuse the old interpretation.
    state["cases"] = [
        {
            "case_id": "case-other",
            "topic": "recharge_failure",
            "issue": {"text": "回充失败"},
            "status": "active",
            "tried_actions": [{"action_id": "clean_contacts", "outcome": "failed"}],
            "not_tried_actions": [],
            "suggested_actions": [],
            "questions": [],
        }
    ]
    state["active_case_id"] = "case-other"
    miss = routing.decide(state, "回充失败，找不到充电座")
    assert miss["route"] == "semantic"
    # No cross-session cache: a fresh state is empty.
    assert support.empty_state()["interpretation_cache"] == []


def test_interpretation_failure_fallback_does_not_fabricate_answers():
    def boom(text, context):
        raise RuntimeError("provider unavailable")

    bundle = interp.interpret_turn("回充失败，找不到充电座", interpreter=boom)
    result = support.apply_user_turn(
        {}, "回充失败，找不到充电座", "m-1", now=1, interpretation=bundle
    )
    state = result["state"]
    case = state["cases"][0]
    assert bundle["trace"]["method"] == "fallback"
    assert (bundle["trace"].get("usage") or None) is None
    assert case["tried_actions"] == []
    rows = case["questions"]
    assert rows and rows[0]["slot"] == "dock_find_referent"
    assert rows[0]["status"] in {"pending", "deferred"}
    assert rows[0]["answer_value"] is None


def test_routing_does_not_swallow_negation_correction_or_explicit_success():
    state = support.empty_state()
    assert routing.decide(state, "我没擦触点，还是不行")["route"] == "semantic"
    assert routing.decide(state, "不对，我说错了，是我找不到充电座")["route"] == "semantic"
    assert routing.decide(state, "能自己回充了")["route"] == "semantic"
    assert routing.decide(state, "谢谢")["route"] == "protocol_ack"
    # Explicit success is allowed as a deterministic slot answer only when the
    # weak-closure slot is actually pending.
    pending = support.empty_state()
    pending = support.apply_user_turn(pending, "回充失败", "m-1", now=1)["state"]
    pending = support.apply_user_turn(pending, "谢谢", "m-2", now=2)["state"]
    assert routing.decide(pending, "能自己回充了")["route"] == "deterministic_slot_answer"


def test_usage_meter_separates_aggregate_events_from_model_requests():
    meter = TurnUsage()
    meter.record(
        layer="interpretation",
        usage={"total_tokens": 983},
        call_id="interpretation-1",
        model_requests=1,
    )
    meter.record(
        layer="main_agent",
        usage={"input_tokens": 2879, "output_tokens": 356, "total_tokens": 3235},
        call_id="main-agent-4",
        model_requests=2,
        aggregated=True,
    )
    meter.record(
        layer="rag_summary",
        usage={"total_tokens": 823},
        call_id="rag_summary-1",
        model_requests=1,
    )
    summary = meter.summary()
    assert summary["model_calls"] == 4
    assert summary["requests"] == 4
    assert summary["ledger_events"] == 3
    assert summary["aggregated_events"] == 1
    assert summary["by_layer"]["main_agent"]["model_calls"] == 2
    assert summary["by_layer"]["main_agent"]["requests"] == 2
    assert summary["by_layer"]["main_agent"]["ledger_events"] == 1
    assert summary["by_layer"]["main_agent"]["total_tokens"] == 3235
    # Tokens are from one aggregate row and are not multiplied by requests.
    assert summary["total"]["total_tokens"] == 983 + 3235 + 823
    # A reused interpretation is a ledger event with zero model requests.
    replay = TurnUsage()
    replay.record(
        layer="interpretation",
        usage=None,
        call_id="interpretation-replay",
        reused=True,
        model_requests=0,
    )
    replay_summary = replay.summary()
    assert replay_summary["model_calls"] == 0
    assert replay_summary["ledger_events"] == 1
    assert replay_summary["usage_status"] == "not_called"
    assert replay_summary["total"]["total_tokens"] is None


def test_usage_meter_counts_each_layer_once_and_never_estimates():
    meter = TurnUsage()
    meter.record(layer="interpretation", usage={"input_tokens": 6, "output_tokens": 4, "total_tokens": 10}, call_id="i1")
    meter.record(layer="main_agent", usage={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}, call_id="m1")
    meter.record(layer="rag_summary", usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}, call_id="r1")
    meter.record(layer="interpretation", usage={"total_tokens": 10}, call_id="i1")  # duplicate
    summary = meter.summary()
    assert summary["total"]["total_tokens"] == 35
    assert summary["usage_status"] == "reported"
    assert summary["cost"] is None
    assert summary["by_layer"]["interpretation"]["total_tokens"] == 10
    assert summary["by_layer"]["main_agent"]["total_tokens"] == 20
    assert summary["by_layer"]["rag_summary"]["total_tokens"] == 5
    # Merge a child summary that repeats r1 and adds r2; no double count.
    child = TurnUsage()
    child.record(layer="rag_summary", usage={"total_tokens": 5}, call_id="r1")
    child.record(layer="rag_summary", usage={"total_tokens": 7}, call_id="r2")
    meter.merge(child.summary())
    assert meter.summary()["total"]["total_tokens"] == 42
    # Missing usage must produce null total, not an estimate.
    partial = TurnUsage()
    partial.record(layer="interpretation", usage=None, call_id="i2")
    partial.record(layer="main_agent", usage={"total_tokens": 20}, call_id="m2")
    psummary = partial.summary()
    assert psummary["usage_status"] == "partial"
    assert psummary["total"]["total_tokens"] is None
    assert TurnUsage().summary()["usage_status"] == "not_called"
    assert TurnUsage().summary()["total"]["total_tokens"] is None


def test_referent_detector_does_not_flag_a_new_symptom_question():
    direct = "先确认一下：是机器人回不到充电座（例如在座前打转、对不上），还是您找不到充电座的位置？"
    repeat = "另外，您说的“找不到充电座”，我还是想确认一下：是机器人自己回不去，还是您找不到充电座放在哪？"
    new_symptom = "机器人现在是在充电座前打转/对不上，还是停在旁边、找不到方向？"
    assert questions.detect_slot_question(direct, "dock_find_referent") is True
    assert questions.detect_slot_question(repeat, "dock_find_referent") is True
    assert questions.detect_slot_question(new_symptom, "dock_find_referent") is False


def test_trace_tool_model_calls_are_not_double_counted_in_main_layer():
    from modules.instances.runtime.execution import _usage

    summary = {
        "model_calls": 3,
        "prompt_tokens": 3000,
        "completion_tokens": 1141,
        "total_tokens": 4141,
        "latency_ms": 5000,
        "model_events": [
            {"node": "model", "prompt_tokens": 1000, "completion_tokens": 349, "total_tokens": 1349},
            {"node": "tools", "prompt_tokens": 600, "completion_tokens": 269, "total_tokens": 869},
            {"node": "model", "prompt_tokens": 1400, "completion_tokens": 523, "total_tokens": 1923},
        ],
    }
    usage = _usage(None, summary)
    main = usage["by_layer"]["main_agent"]
    assert main["model_calls"] == 2
    assert main["total_tokens"] == 3272
    assert main["tool_model_calls_in_trace"] == 1
    assert main["tool_model_tokens_in_trace"] == 869
    assert usage["trace_total_tokens"] == 4141


def test_legacy_resolved_ambiguity_migrates_to_answered_question_without_losing_facts():
    legacy = {
        "schema_version": 2,
        "turn": 2,
        "active_case_id": "case-old",
        "cases": [
            {
                "case_id": "case-old",
                "topic": "recharge_failure",
                "issue": {"text": "回充失败"},
                "status": "active",
                "stage": "diagnose",
                "work_context": {
                    "topic": "recharge_failure",
                    "ambiguity": [
                        {
                            "id": "dock_find_referent",
                            "candidates": ["robot_cannot_return", "user_cannot_find_dock"],
                            "status": "resolved",
                            "basis_quote": "机器人回不去了",
                            "resolution": "robot_cannot_return",
                            "resolved_turn": 2,
                        }
                    ],
                },
                "tried_actions": [
                    {
                        "action_id": "clear_dock_obstacles",
                        "label": "清理充电座周围障碍物",
                        "outcome": "failed",
                        "user_executed": True,
                        "source_message_id": "m1",
                        "source_turn": 1,
                        "evidence": "清理了还是不行",
                    }
                ],
                "not_tried_actions": [],
                "suggested_actions": [],
                "sources": [],
                "created_at": 1,
                "updated_at": 2,
            }
        ],
        "updated_at": 2,
    }
    state = support.load_state(legacy, now=3)
    case = state["cases"][0]
    row = questions.find_question(case, "dock_find_referent")
    assert row["status"] == "answered"
    assert row["answer_value"] == "robot_cannot_return"
    assert case["tried_actions"][0]["action_id"] == "clear_dock_obstacles"
    prompt = support.prompt_context(state)
    assert "dock_find_referent=robot_cannot_return" in prompt
    assert "仍未知" not in prompt


def test_session_state_roundtrip_keeps_question_records():
    state = support.empty_state()
    state = support.apply_user_turn(state, "回充失败，找不到充电座", "m-1", now=1)["state"]
    state = support.apply_assistant_turn(
        state, str(support._DOCK_AMBIGUITY_QUESTION), "a-1", turn=1, now=2
    )
    restored = support.load_state(json.loads(json.dumps(state, ensure_ascii=False)))
    row = questions.find_question(restored["cases"][0], "dock_find_referent")
    assert row["ask_count"] == 1
    assert row["asked_turn"] == 1
    assert restored["last_turn_scope"] == "recharge"
