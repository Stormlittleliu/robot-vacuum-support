# -*- coding: utf-8 -*-
"""First-turn activation / feedback-shortcut oracle tests (development only).

These are same-family migration checks plus counterexamples.  They are not
holdout, not a success rate, and they do not read or change reviewed cases,
old runs/results, the Judge, GUI, assets or support_state fixtures.
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

from robot_vacuum_support import state as support  # noqa: E402

POWER_FIRST_TURN = "充电座指示灯不亮，机器人放上去没反应，怎么办？"
RECHARGE_VAGUE = "它回充不行，帮我看看。"


def semantic(**overrides):
    """A validated-shape semantic proposal; state still re-validates quotes."""
    payload = {
        "domain": "robot_vacuum_after_sales",
        "topic": "recharge_failure",
        "objects": ["robot", "charging_dock"],
        "experiencer": "robot",
        "observable_symptom": {"text": "", "observed_by_user": True, "safety": "none"},
        "ambiguity": [],
        "executed_actions": [],
        "not_tried_actions": [],
        "interaction_stage": "first_report",
        "familiarity": "unknown",
        "closure_signal": "none",
        "information_sources": ["user_message"],
        "unknowns": [],
        "topic_switch": False,
        "wants_full_checklist": False,
    }
    payload.update(overrides)
    if not payload["observable_symptom"].get("text"):
        payload["observable_symptom"] = {
            "text": overrides.get("_symptom_text", RECHARGE_VAGUE),
            "observed_by_user": True,
            "safety": "none",
        }
    return payload


def apply(state, text, message_id, *, now, interpretation=None):
    return support.apply_user_turn(
        state,
        text,
        message_id,
        now=now,
        interpretation=interpretation,
    )


def active(state):
    return support.public_view(state)["active_case"]


def test_a_first_turn_symptom_uses_agent_path_not_followup_clarification():
    result = apply(
        support.empty_state(),
        POWER_FIRST_TURN,
        "m1",
        now=1,
        interpretation=semantic(
            observable_symptom={
                "text": "充电座指示灯不亮，机器人放上去没反应",
                "observed_by_user": True,
                "safety": "none",
            }
        ),
    )
    case = active(result["state"])
    assert result["scope"] == "recharge"
    assert result.get("direct_answer") is None
    assert case is not None and case["status"] == "active"
    assert case["tried_actions"] == []
    assert (case.get("next_step") or {}).get("action_id") == "check_power"
    assert (case.get("next_step") or {}).get("kind") == "action"
    assert "还是不行" not in (result.get("direct_answer") or "")


def test_first_turn_different_symptom_is_not_a_feedback_turn():
    result = apply(
        support.empty_state(),
        "回充失败，机器人老在充电座前面打转，怎么办？",
        "m1",
        now=1,
    )
    case = active(result["state"])
    assert result["scope"] == "recharge"
    assert result.get("direct_answer") is None
    assert case is not None and case["status"] == "active"
    assert case["tried_actions"] == []
    assert (case.get("next_step") or {}).get("action_id") in {"clean_sensors", "reseat_dock"}
    assert "还是不行" not in (result.get("direct_answer") or "")


def test_first_turn_safety_symptom_uses_safety_step_not_feedback():
    result = apply(
        support.empty_state(),
        "回充时充电座冒烟了，还有焦味",
        "m1",
        now=1,
    )
    case = active(result["state"])
    assert result.get("direct_answer") is None
    assert case is not None and case["status"] == "active"
    assert (case.get("next_step") or {}).get("action_id") == "safety_check"


def test_first_turn_with_historical_execution_fact_is_recorded_without_stale_ask():
    result = apply(
        support.empty_state(),
        "回充失败，我已经擦了充电触点，还是不行",
        "m1",
        now=1,
    )
    case = active(result["state"])
    assert result["scope"] == "recharge"
    assert result.get("direct_answer") is None
    assert case is not None and case["status"] == "active"
    assert [[item["action_id"], item["outcome"]] for item in case["tried_actions"]] == [
        ["clean_contacts", "failed"]
    ]
    assert "还是不行" not in (result.get("direct_answer") or "")


def test_bare_followup_after_a_unique_suggestion_links_failed_without_direct_ask():
    state = apply(support.empty_state(), "回充失败", "m1", now=1)["state"]
    state = support.apply_assistant_turn(
        state, "先清理充电座周围障碍物，再观察。[故障排除.txt]", "a1", turn=1, now=2
    )
    case = active(state)
    assert case["tried_actions"] == []
    assert [item["action_id"] for item in case["suggested_actions"]] == ["clear_dock_obstacles"]

    result = apply(state, "还是不行", "m2", now=3)
    case = active(result["state"])
    assert result.get("direct_answer") is None
    assert [[item["action_id"], item["outcome"]] for item in case["tried_actions"]] == [
        ["clear_dock_obstacles", "failed"]
    ]


def test_multiple_pending_suggestions_must_be_confirmed_not_guessed():
    state = apply(support.empty_state(), "回充失败", "m1", now=1)["state"]
    state = support.apply_assistant_turn(
        state, "先清理充电座周围障碍物。[故障排除.txt]", "a1", turn=1, now=2
    )
    case = state["cases"][0]
    case["suggested_actions"] = [
        {
            "action_id": "clean_contacts",
            "label": "清洁充电触点",
            "primary": True,
            "status": "suggested",
            "source_turn": 1,
        },
        {
            "action_id": "check_power",
            "label": "检查充电座电源",
            "primary": False,
            "status": "suggested",
            "source_turn": 1,
        },
    ]

    result = apply(state, "还是不行", "m2", now=3)
    case = active(result["state"])
    direct = result.get("direct_answer") or ""
    assert "哪一步" in direct
    assert case["tried_actions"] == []
    assert case["status"] == "active"


def test_later_new_symptom_does_not_reuse_the_old_which_step_prompt():
    state = apply(support.empty_state(), "回充失败", "m1", now=1)["state"]
    state = support.apply_assistant_turn(
        state, "先检查充电座电源。[故障排除.txt]", "a1", turn=1, now=2
    )
    state = apply(state, "检查了，还是不行", "m2", now=3)["state"]
    case = active(state)
    assert [[item["action_id"], item["outcome"]] for item in case["tried_actions"]] == [
        ["clear_dock_obstacles", "failed"]
    ]

    result = apply(state, "现在充电座指示灯不亮，机器人放上去没反应", "m3", now=4)
    case = active(result["state"])
    assert result.get("direct_answer") is None
    assert case is not None and case["status"] == "active"
    assert [[item["action_id"], item["outcome"]] for item in case["tried_actions"]] == [
        ["clear_dock_obstacles", "failed"]
    ]


def test_semantic_recharge_failure_without_state_keyword_activates_case():
    bundle = semantic(
        _symptom_text="回充不行",
        ambiguity=[
            {
                "id": "dock_find_referent",
                "candidates": ["robot_cannot_return", "user_cannot_find_dock"],
                "status": "open",
                "basis_quote": "它回充不行",
            }
        ],
    )
    result = apply(support.empty_state(), RECHARGE_VAGUE, "m1", now=1, interpretation=bundle)
    case = active(result["state"])
    assert result["scope"] == "recharge"
    assert case is not None and case["status"] == "active"
    assert case["tried_actions"] == []
    context = case.get("work_context") or {}
    assert context.get("topic") == "recharge_failure"
    assert context.get("ambiguity") and context["ambiguity"][0]["id"] == "dock_find_referent"
    # The existing one-time ambiguity policy may ask the binary question; it must
    # not fabricate a tried step or emit the stale follow-up text.
    direct = result.get("direct_answer") or ""
    assert "还是不行”是指" not in direct
    assert case["tried_actions"] == []


def test_resolved_referent_still_activates_the_recharge_case():
    bundle = semantic(
        _symptom_text="回充不行",
        ambiguity=[
            {
                "id": "dock_find_referent",
                "candidates": ["robot_cannot_return", "user_cannot_find_dock"],
                "status": "resolved",
                "basis_quote": "它回充不行",
                "resolution": "robot_cannot_return",
            }
        ],
    )
    result = apply(support.empty_state(), RECHARGE_VAGUE, "m1", now=1, interpretation=bundle)
    case = active(result["state"])
    assert result["scope"] == "recharge"
    assert case is not None and case["status"] == "active"
    assert case["tried_actions"] == []


def test_text_only_semantic_failure_without_failure_word_activates_case():
    result = apply(support.empty_state(), RECHARGE_VAGUE, "m1", now=1)
    case = active(result["state"])
    assert result["scope"] == "recharge"
    assert case is not None and case["status"] == "active"
    assert case["tried_actions"] == []
    assert result.get("direct_answer") is None


def test_semantically_explicit_failure_with_unusual_words_activates_case():
    text = "回充这块它不太灵，帮我看看。"
    bundle = semantic(
        _symptom_text="回充不太灵",
        closure_signal="failure",
        interaction_stage="first_report",
    )
    result = apply(support.empty_state(), text, "m1", now=1, interpretation=bundle)
    case = active(result["state"])
    assert result["scope"] == "recharge"
    assert case is not None and case["status"] == "active"
    assert case["tried_actions"] == []


@pytest.mark.parametrize(
    "text",
    ["怎么设置自动回充？", "自动回充怎么开启？", "APP里自动回充在哪设置？"],
)
def test_settings_questions_do_not_create_a_fault_case(text):
    result = apply(support.empty_state(), text, "m1", now=1)
    assert result["state"]["cases"] == []
    assert active(result["state"]) is None
    assert result.get("direct_answer") is None


@pytest.mark.parametrize(
    "text",
    ["怎么设置自动回充？", "自动回充怎么开启？"],
)
def test_settings_question_with_recharge_topic_but_no_fault_evidence_is_not_activated(text):
    bundle = semantic(
        _symptom_text=text,
        closure_signal="none",
        ambiguity=[],
        interaction_stage="first_report",
    )
    result = apply(support.empty_state(), text, "m1", now=1, interpretation=bundle)
    assert result["state"]["cases"] == []
    assert active(result["state"]) is None
    assert result.get("direct_answer") is None


def test_settings_question_with_spurious_referent_ambiguity_is_not_activated():
    text = "怎么设置自动回充？"
    bundle = semantic(
        _symptom_text=text,
        closure_signal="none",
        interaction_stage="first_report",
        ambiguity=[
            {
                "id": "dock_find_referent",
                "candidates": ["robot_cannot_return", "user_cannot_find_dock"],
                "status": "open",
                "basis_quote": text,
            }
        ],
    )
    result = apply(support.empty_state(), text, "m1", now=1, interpretation=bundle)
    assert result["state"]["cases"] == []
    assert active(result["state"]) is None
    assert result.get("direct_answer") is None


def test_ask_about_old_steps_uses_evidence_not_invention():
    state = apply(support.empty_state(), "回充失败", "m1", now=1)["state"]
    state = support.apply_assistant_turn(
        state, "先清理充电座周围障碍物。[故障排除.txt]", "a1", turn=1, now=2
    )
    first = apply(state, "我刚才试过什么？", "m2", now=3)
    assert "还没有你确认执行过" in (first.get("direct_answer") or "")
    assert active(first["state"])["tried_actions"] == []

    state = support.apply_assistant_turn(
        first["state"], "按记录继续。", "a2", turn=2, now=4
    )
    state = apply(state, "我擦了触点", "m3", now=5)["state"]
    second = apply(state, "我都试过哪些？", "m4", now=6)
    direct = second.get("direct_answer") or ""
    assert "清洁充电触点" in direct
    assert "结果未说明" in direct
    assert "还是不行”是指" not in direct
    assert [[item["action_id"], item["outcome"]] for item in active(second["state"])["tried_actions"]] == [
        ["clean_contacts", "unknown"]
    ]


def test_duplicate_message_is_idempotent_and_state_survives_reload():
    first = apply(support.empty_state(), POWER_FIRST_TURN, "m1", now=1)
    snapshot = json.dumps(first["state"], ensure_ascii=False, sort_keys=True)
    replay = apply(first["state"], POWER_FIRST_TURN, "m1", now=2)
    assert replay["changed"] is False
    assert json.dumps(replay["state"], ensure_ascii=False, sort_keys=True) == snapshot

    class FakeConfig:
        instance_id = "inst-first-turn-test"
        artifact_id = "art-first-turn-test"
        scenario = "robot_vacuum"

        def __init__(self, root: Path):
            self.sessions_root = root / "sessions"
            self.storage_root = root

    from modules.instances.runtime.store import SessionStore

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(FakeConfig(Path(tmp)))
        session = store.create(workflow="agent")["session_id"]
        store.save_support_state(session, first["state"])
        reopened = SessionStore(FakeConfig(Path(tmp)))
        loaded = support.load_state(reopened.load_support_state(session))
        case = active(loaded)
        assert case is not None and case["status"] == "active"
        assert (case.get("next_step") or {}).get("action_id") == "check_power"
        replay_after_reload = support.apply_user_turn(
            loaded, POWER_FIRST_TURN, "m1", now=9
        )
        assert replay_after_reload["changed"] is False


def test_topic_switch_return_thanks_and_correction_keep_one_case_consistent():
    state = apply(support.empty_state(), POWER_FIRST_TURN, "m1", now=1)["state"]
    case_id = active(state)["case_id"]
    state = support.apply_assistant_turn(
        state, "先检查充电座电源。[故障排除.txt]", "a1", turn=1, now=2
    )

    switched = apply(state, "拖地时水箱漏水怎么办？", "m2", now=3)
    assert switched["scope"] == "other"
    assert active(switched["state"])["case_id"] == case_id
    assert switched["state"]["open_tasks"][0]["status"] == "open"
    assert switched["state"]["pending_confirmation"]

    returned = apply(switched["state"], "还是回充那个问题", "m3", now=4)
    assert returned["scope"] == "recharge"
    assert active(returned["state"])["case_id"] == case_id

    tried = apply(returned["state"], "我擦了触点", "m4", now=5)["state"]
    assert [[item["action_id"], item["outcome"]] for item in active(tried)["tried_actions"]] == [
        ["clean_contacts", "unknown"]
    ]
    tried = support.apply_assistant_turn(tried, "确认触点结果。[维护保养.txt]", "a4", turn=4, now=6)

    corrected = apply(tried, "不对，我说错了，我没擦触点", "m5", now=7)
    case = active(corrected["state"])
    assert case["case_id"] == case_id
    assert case["tried_actions"] == []
    assert "clean_contacts" in [item["action_id"] for item in case["not_tried_actions"]]

    thanked = apply(corrected["state"], "谢谢", "m6", now=8)
    case = active(thanked["state"])
    assert case["case_id"] == case_id
    assert case["status"] == "active"
    assert (case.get("closure_evidence") or {}).get("level") == "weak_positive"

    snapshot = json.dumps(thanked["state"], ensure_ascii=False, sort_keys=True)
    duplicate = apply(thanked["state"], "谢谢", "m6", now=9)
    assert duplicate["changed"] is False
    assert json.dumps(duplicate["state"], ensure_ascii=False, sort_keys=True) == snapshot
