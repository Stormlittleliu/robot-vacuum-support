# -*- coding: utf-8 -*-
"""IP-1 development migration tests: work context, ambiguity, progressive diagnosis.

These are development migration checks, not holdout and not a success rate.
They must not modify reviewed datasets or user reviews.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "tests"))

from robot_vacuum_support import interpretation as interp  # noqa: E402
from robot_vacuum_support import state as support  # noqa: E402

CASES_PATH = ROOT / "eval" / "development-next" / "cases.json"


def load_cases() -> list[dict]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert payload["split"] == "development"
    assert payload["schema_version"] == "context-diagnosis-development/v1"
    return list(payload["cases"])


def play(turns: list[str], interpretation=None):
    state = support.empty_state()
    result = None
    for index, text in enumerate(turns, start=1):
        result = support.apply_user_turn(
            state,
            text,
            f"m-{index}",
            now=100 + index,
            interpretation=(interpretation or {}).get(index - 1),
        )
        state = result["state"]
        if result.get("direct_answer"):
            state = support.apply_assistant_turn(
                state,
                result["direct_answer"],
                f"a-{index}",
                turn=state.get("turn"),
                now=200 + index,
            )
    return state, result


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
def test_development_migration_case(case):
    state, _result = play(case["turns"])
    view = support.public_view(state)
    active = view["active_case"]
    expected = case["expect"]
    if "scope" in expected:
        assert _result["scope"] == expected["scope"], case["id"]
    if "has_case" in expected:
        assert (active is not None) == expected["has_case"], case["id"]
    if active is None:
        return
    if "status" in expected:
        assert active["status"] == expected["status"], case["id"]
    if "tried" in expected:
        assert [[item["action_id"], item["outcome"]] for item in active["tried_actions"]] == expected["tried"], case["id"]
    if "not_tried" in expected:
        assert [item["action_id"] for item in active["not_tried_actions"]] == expected["not_tried"], case["id"]
    if "next_kind" in expected:
        assert (active.get("next_step") or {}).get("kind") == expected["next_kind"], case["id"]
    if "next_action" in expected:
        assert (active.get("next_step") or {}).get("action_id") == expected["next_action"], case["id"]
    if expected.get("single_next_step"):
        assert isinstance(active.get("next_step"), dict)
    if "experiencer" in expected:
        assert (active.get("work_context") or {}).get("experiencer") == expected["experiencer"], case["id"]
    if "familiarity" in expected:
        assert (active.get("work_context") or {}).get("familiarity") == expected["familiarity"], case["id"]
    if "closure_level" in expected:
        assert (active.get("closure_evidence") or {}).get("level") == expected["closure_level"], case["id"]
    if "old_task_status" in expected:
        task = next(item for item in view["open_tasks"] if item.get("case_id") == active["case_id"])
        assert task["status"] == expected["old_task_status"], case["id"]
    if expected.get("pending_confirmation"):
        assert view["pending_confirmation"], case["id"]
    if "old_tried" in expected:
        assert [[item["action_id"], item["outcome"]] for item in active["tried_actions"]] == expected["old_tried"], case["id"]
    if "open_task_status" in expected:
        task = next(item for item in view["open_tasks"] if item.get("case_id") == active["case_id"])
        assert task["status"] == expected["open_task_status"], case["id"]
    if "ambiguity" in expected:
        items = (active.get("work_context") or {}).get("ambiguity") or []
        if expected["ambiguity"] is None:
            assert items == [], case["id"]
        else:
            found = next((item for item in items if item.get("id") == expected["ambiguity"]["id"]), None)
            assert found is not None, case["id"]
            assert found.get("status") == expected["ambiguity"]["status"], case["id"]
    if expected.get("direct_contains_all"):
        direct = _result.get("direct_answer") or ""
        for fragment in expected["direct_contains_all"]:
            assert fragment in direct, (case["id"], fragment, direct)
    if expected.get("forbid_auto_action"):
        assert (active.get("next_step") or {}).get("action_id") != expected["forbid_auto_action"], case["id"]


def test_semantic_equivalents_enter_same_recharge_scope():
    for text in (
        "回充失败，回不去了",
        "机器人充不上电，回不了充",
        "回充失败，回不到充电座",
    ):
        result = support.apply_user_turn({}, text, message_id="probe", now=1)
        active = support.public_view(result["state"])["active_case"]
        assert result["scope"] == "recharge", text
        assert active is not None and active["status"] == "active", text


def test_clear_context_does_not_ask_referent_again():
    state, result = play(["回充失败，机器人老在充电座前面转，就是对不上"])
    active = support.public_view(state)["active_case"]
    assert result.get("direct_answer") is None
    assert (active.get("next_step") or {}).get("kind") == "action"
    assert (active.get("work_context") or {}).get("experiencer") == "robot"


def test_ambiguity_question_is_asked_once_not_every_turn():
    state, first = play(["回充失败，找不到充电座"])
    assert first.get("direct_answer")
    assert "机器人" in first["direct_answer"] and "位置" in first["direct_answer"]
    # User ignores the binary question and repeats only "还是不行": no loop.
    second = support.apply_user_turn(state, "还是不行", "m-2", now=3)
    active = support.public_view(second["state"])["active_case"]
    assert second.get("direct_answer") is None
    ambiguity = (active.get("work_context") or {}).get("ambiguity") or []
    assert ambiguity and ambiguity[0]["status"] == "deferred"


def test_unexecuted_step_is_not_promoted_even_if_model_says_so():
    raw = {
        "domain": "robot_vacuum_after_sales",
        "topic": "recharge_failure",
        "executed_actions": [
            {"action_id": "clean_contacts", "outcome": "failed", "evidence_quote": "我擦了触点"}
        ],
        "not_tried_actions": [
            {"action_id": "clean_contacts", "evidence_quote": "没擦触点"}
        ],
    }
    result = support.apply_user_turn(
        {},
        "回充失败，我没擦触点，还是不行",
        "m1",
        now=1,
        interpretation=raw,
    )
    active = support.public_view(result["state"])["active_case"]
    assert active["tried_actions"] == []
    assert [item["action_id"] for item in active["not_tried_actions"]] == ["clean_contacts"]


def test_assistant_suggestion_and_user_feedback_are_distinct():
    state = support.empty_state()
    state = support.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
    state = support.apply_assistant_turn(state, "先清理充电座周围障碍物。", "a1", turn=1, now=2)
    active = support.public_view(state)["active_case"]
    assert active["tried_actions"] == []
    assert active["feedback_events"] == []
    assert [item["action_id"] for item in active["suggested_actions"]] == ["clear_dock_obstacles"]
    state = support.apply_user_turn(state, "清理了，还是不行", "m2", now=3)["state"]
    active = support.public_view(state)["active_case"]
    assert [[item["action_id"], item["outcome"]] for item in active["tried_actions"]] == [
        ["clear_dock_obstacles", "failed"]
    ]
    assert active["feedback_events"][-1]["outcome"] == "failure"


def test_action_result_unknown_is_not_inferred_from_initial_failure():
    result = support.apply_user_turn(
        {}, "回充失败，已经清理了充电座周围", "m1", now=1
    )
    active = support.public_view(result["state"])["active_case"]
    assert [[item["action_id"], item["outcome"]] for item in active["tried_actions"]] == [
        ["clear_dock_obstacles", "unknown"]
    ]
    assert (active.get("next_step") or {}).get("kind") == "confirm"


def test_natural_failure_phrase_after_tried_steps_is_failed():
    state = support.empty_state()
    state = support.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
    state = support.apply_user_turn(
        state,
        "我已经清理了充电座周围，也擦了触点，还是回不去",
        "m2",
        now=2,
    )["state"]
    active = support.public_view(state)["active_case"]
    assert [[item["action_id"], item["outcome"]] for item in active["tried_actions"]] == [
        ["clear_dock_obstacles", "failed"],
        ["clean_contacts", "failed"],
    ]


def test_weak_closure_and_silence_do_not_resolve():
    state, _ = play(["回充失败", "谢谢"])
    active = support.public_view(state)["active_case"]
    assert active["status"] == "active"
    assert (active.get("closure_evidence") or {}).get("level") == "weak_positive"
    empty = support.apply_user_turn(state, "", "m-empty", now=9)
    assert support.public_view(empty["state"])["active_case"]["status"] == "active"


def test_explicit_success_only_resolves_with_user_evidence():
    state, _ = play(["回充失败", "清完能自己回充了"])
    active = support.public_view(state)["active_case"]
    assert active["status"] == "resolved"
    assert (active.get("closure_evidence") or {}).get("level") == "explicit_success"


def test_legacy_schema_is_lazily_migrated_without_losing_facts():
    legacy = {
        "schema_version": 1,
        "turn": 2,
        "active_case_id": "case-old",
        "last_retrieval": None,
        "cases": [
            {
                "case_id": "case-old",
                "topic": "recharge_failure",
                "issue": {"text": "回充失败"},
                "status": "active",
                "stage": "diagnose",
                "tried_actions": [
                    {
                        "action_id": "clean_contacts",
                        "label": "清洁充电触点",
                        "outcome": "failed",
                        "user_executed": True,
                        "source_message_id": "m1",
                        "source_turn": 1,
                        "evidence": "擦了触点还是不行",
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
    assert state["schema_version"] == 2
    assert state["migration"]["from"] == 1
    assert state["cases"][0]["tried_actions"][0]["action_id"] == "clean_contacts"
    assert state["cases"][0]["tried_actions"][0]["outcome"] == "failed"
    assert state["open_tasks"][0]["case_id"] == "case-old"
    assert state["open_tasks"][0]["status"] == "open"


def test_interpretation_failure_falls_back_and_records_trace():
    def boom(text, context):
        raise RuntimeError("provider unavailable")

    bundle = interp.interpret_turn("回充失败，找不到充电座", interpreter=boom)
    assert bundle["trace"]["method"] == "fallback"
    assert bundle["trace"]["fallback_used"] is True
    assert "provider unavailable" in (bundle["trace"]["fallback_reason"] or "")
    assert bundle["interpretation"]["ambiguity"][0]["id"] == "dock_find_referent"


def test_model_proposal_quote_must_exist_in_user_message():
    raw = {
        "domain": "robot_vacuum_after_sales",
        "topic": "recharge_failure",
        "executed_actions": [
            {"action_id": "clean_contacts", "outcome": "failed", "evidence_quote": "我擦了触点"}
        ],
        "information_sources": ["assistant_suggestion"],
    }
    normalized, errors = interp.normalize_interpretation(raw, "回充失败，还没排查")
    assert normalized["executed_actions"] == []
    assert any("quote not in user message" in item for item in errors)
    assert any("information_sources" in item for item in errors)


def test_bracketed_ipv6_no_proxy_is_removed_before_interpreter(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    interp._sanitize_no_proxy()
    value = interp.os.environ["NO_PROXY"]
    assert "[::1]" not in value
    assert "::1" in value
    assert "localhost" in value


def test_interpretation_usage_is_reported_not_invented():
    def fake(text, context):
        return {
            "payload": {
                "domain": "robot_vacuum_after_sales",
                "topic": "recharge_failure",
                "experiencer": "ambiguous",
                "ambiguity": [
                    {
                        "id": "dock_find_referent",
                        "candidates": ["robot_cannot_return", "user_cannot_find_dock"],
                        "status": "open",
                        "basis_quote": "找不到充电座",
                    }
                ],
            },
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
            "model": "fake-structured",
            "attempts": 1,
        }

    bundle = interp.interpret_turn("回充失败，找不到充电座", interpreter=fake)
    assert bundle["trace"]["usage_status"] == "reported"
    assert bundle["trace"]["usage"]["total_tokens"] == 20
    assert bundle["trace"]["model"] == "fake-structured"


def test_duplicate_request_is_idempotent():
    state = support.apply_user_turn({}, "回充失败", "m1", now=1)["state"]
    snapshot = json.dumps(state, ensure_ascii=False, sort_keys=True)
    replay = support.apply_user_turn(state, "回充失败", "m1", now=2)
    assert replay["changed"] is False
    assert json.dumps(replay["state"], ensure_ascii=False, sort_keys=True) == snapshot


class FakeConfig:
    instance_id = "inst-context-test"
    artifact_id = "art-context-test"
    scenario = "robot_vacuum"

    def __init__(self, root: Path):
        self.sessions_root = root / "sessions"
        self.storage_root = root


def test_session_scope_isolation_with_store():
    from modules.instances.runtime.store import SessionStore

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(FakeConfig(Path(tmp)))
        first = store.create(workflow="agent")["session_id"]
        second = store.create(workflow="agent")["session_id"]
        state = support.apply_user_turn({}, "回充失败", "m1", now=1)["state"]
        store.save_support_state(first, state)
        assert store.load_support_state(second) is None
        loaded = support.load_state(store.load_support_state(first))
        assert support.public_view(loaded)["active_case"]["issue"] == "回充失败"
