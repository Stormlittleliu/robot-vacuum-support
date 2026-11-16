# -*- coding: utf-8 -*-
"""Diagnostic scope oracle tests (development only).

Covers observation -> capability scope -> hypothesis -> closure, the
manual/automatic separation, retraction/idempotency, semantic rejection and
old-schema compatibility.  It is not holdout and does not score answer text.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from robot_vacuum_support import diagnostic_scope as scope  # noqa: E402
from robot_vacuum_support import interpretation as interp  # noqa: E402
from robot_vacuum_support import state as support  # noqa: E402

S1 = "自动回充还是失败，但我手动把机器人放到充电座上就能充电。是不是证明触点问题已经解决，接下来不用管了？"
S2 = "我已经清了充电座周围，手动放回可以充电，但还没试过让它自己回去。现在能确定就是遮挡造成的吗？"
S3 = "机器人自己回不到充电座，我把它放上去也充不上电。该先确认什么？"


def apply(state, text, message_id, *, now, interpretation=None):
    return support.apply_user_turn(
        state,
        text,
        message_id,
        now=now,
        interpretation=interpretation,
    )


def case(state):
    return support.public_view(state)["active_case"]


def by_capability(state):
    active = case(state)
    if active is None:
        return {}
    return {
        row["capability"]: row
        for row in (active.get("observations") or [])
        if row.get("fact_status", "effective") == "effective"
    }


def semantic(**overrides):
    payload = {
        "domain": "robot_vacuum_after_sales",
        "topic": "recharge_failure",
        "objects": ["robot", "charging_dock"],
        "experiencer": "robot",
        "observable_symptom": {"text": "回充失败", "observed_by_user": True, "safety": "none"},
        "ambiguity": [],
        "executed_actions": [],
        "not_tried_actions": [],
        "capability_observations": [],
        "observation_retractions": [],
        "interaction_stage": "first_report",
        "familiarity": "unknown",
        "closure_signal": "none",
        "information_sources": ["user_message"],
        "unknowns": [],
        "topic_switch": False,
        "wants_full_checklist": False,
    }
    payload.update(overrides)
    return payload


class TestObservationScope:
    def test_manual_works_auto_fails_plain_stays_active(self):
        state = apply(support.empty_state(), "自动回充失败，但手动放回充电座能充电", "m1", now=1)["state"]
        active = case(state)
        observations = by_capability(state)
        assert active["status"] == "active"
        assert active["resolved_at"] is None
        assert observations["manual_charging"]["result"] == "works"
        assert observations["automatic_return"]["result"] == "fails"
        assert active["closure_evidence"]["level"] == "partial_success"
        diagnostic = active["diagnostic_scope"]
        assert diagnostic["task_goal"] == "automatic_return"
        assert diagnostic["task_goal_status"] == "fails"
        assert diagnostic["allowed_conclusion_strength"] == "task_goal_failed"
        assert diagnostic["root_cause"]["status"] == "unconfirmed"
        assert all(row["status"] == "hypothesis" for row in diagnostic["root_cause"]["candidates"])
        assert diagnostic["resolved"] is False

    def test_induced_exclusive_cause_question_does_not_turn_into_fact(self):
        state = apply(support.empty_state(), S1, "m1", now=1)["state"]
        active = case(state)
        observations = by_capability(state)
        assert active["status"] == "active"
        assert observations["manual_charging"]["result"] == "works"
        assert observations["automatic_return"]["result"] == "fails"
        assert active["diagnostic_scope"]["resolved"] is False
        # The conditional "是不是证明..." clause must not become an observation.
        assert all("是不是" not in row["quote"] for row in active["observations"])
        assert all("不用管" not in row["quote"] for row in active["observations"])

    def test_manual_and_auto_fail_keeps_case_active(self):
        state = apply(support.empty_state(), S3, "m1", now=1)["state"]
        active = case(state)
        observations = by_capability(state)
        assert active["status"] == "active"
        assert observations["manual_charging"]["result"] == "fails"
        assert observations["automatic_return"]["result"] == "fails"
        assert active["diagnostic_scope"]["allowed_conclusion_strength"] == "task_goal_failed"
        assert active["diagnostic_scope"]["resolved"] is False

    def test_clearance_manual_only_leaves_auto_unverified(self):
        state = apply(support.empty_state(), S2, "m1", now=1)["state"]
        active = case(state)
        observations = by_capability(state)
        assert active["status"] == "active"
        assert observations["manual_charging"]["result"] == "works"
        assert "automatic_return" not in observations
        diagnostic = active["diagnostic_scope"]
        assert diagnostic["task_goal_status"] == "unknown"
        assert diagnostic["allowed_conclusion_strength"] == "scoped_positive_only"
        assert "automatic_return" in diagnostic["unverified_capabilities"]
        assert diagnostic["resolved"] is False
        assert active["closure_evidence"]["level"] == "partial_success"

    def test_contact_cleaning_is_action_not_effect(self):
        state = apply(support.empty_state(), "回充失败，我已经擦了充电触点", "m1", now=1)["state"]
        active = case(state)
        observations = by_capability(state)
        assert "manual_charging" not in observations
        assert all(row["result"] != "works" for row in observations.values())
        assert active["status"] == "active"
        assert active["diagnostic_scope"]["allowed_conclusion_strength"] == "task_goal_failed"
        assert ("clean_contacts", "unknown") in [
            (row["action_id"], row["outcome"]) for row in active["tried_actions"]
        ]

    def test_explicit_auto_recovery_resolves(self):
        state = apply(support.empty_state(), "自动回充失败，回不到充电座。", "m1", now=1)["state"]
        state = apply(state, "现在它能从房间自己找到充电座，并且已经开始充电了。", "m2", now=2)["state"]
        active = case(state)
        assert active["status"] == "resolved"
        assert active["resolved_at"] is not None
        assert by_capability(state)["automatic_return"]["result"] == "works"
        assert active["diagnostic_scope"]["allowed_conclusion_strength"] == "resolved"
        assert active["diagnostic_scope"]["resolved"] is True

    def test_hypothetical_and_other_source_are_not_observations(self):
        state = apply(
            support.empty_state(),
            "别人说手动放回能充电，如果能充是不是说明触点没问题？",
            "m1",
            now=1,
        )["state"]
        active = case(state)
        assert by_capability(state) == {}
        assert active["diagnostic_scope"]["allowed_conclusion_strength"] != "resolved"
        assert active["status"] != "resolved"

    def test_pure_settings_and_thanks_create_no_capability_fact(self):
        for text in ("怎么设置自动回充？", "谢谢"):
            state = apply(support.empty_state(), text, "m1", now=1)["state"]
            assert by_capability(state) == {}
            active = case(state)
            if active is not None:
                assert active["diagnostic_scope"]["resolved"] is False


class TestClosureGate:
    def test_manual_success_with_global_closure_signal_does_not_resolve(self):
        state = apply(support.empty_state(), "自动回充失败。", "m1", now=1)["state"]
        payload = semantic(
            closure_signal="explicit_success",
            interaction_stage="resolved",
            capability_observations=[
                {
                    "capability": "automatic_return",
                    "result": "works",
                    "evidence_quote": "我手动把它放回充电座，充上电了",
                    "source": "user_message",
                }
            ],
        )
        result = apply(state, "我手动把它放回充电座，充上电了", "m2", now=2, interpretation=payload)
        state = result["state"]
        active = case(state)
        observations = by_capability(state)
        assert active["status"] == "active"
        assert active["resolved_at"] is None
        assert observations["manual_charging"]["result"] == "works"
        assert observations.get("automatic_return", {}).get("result") != "works"
        assert active["closure_evidence"]["level"] == "partial_success"
        assert active["closure_evidence"]["scope_reason"] == (
            "manual_observation_does_not_cover_automatic_return"
        )
        errors = state["last_interpretation"]["trace"]["validation_errors"]
        assert any("capability_observation" in error for error in errors)
        assert active["diagnostic_scope"]["resolved"] is False

    def test_auto_success_can_close_after_manual_observation(self):
        state = apply(support.empty_state(), S1, "m1", now=1)["state"]
        state = apply(state, "现在它能从房间自己找到充电座，并且已经开始充电了。", "m2", now=2)["state"]
        active = case(state)
        assert active["status"] == "resolved"
        assert by_capability(state)["automatic_return"]["result"] == "works"
        assert active["diagnostic_scope"]["resolved"] is True


class TestRetractionAndIdempotency:
    def test_retraction_keeps_audit_and_does_not_resurrect(self):
        state = apply(
            support.empty_state(),
            "自动回充失败，我手动放回充电座能充电",
            "m1",
            now=1,
        )["state"]
        assert by_capability(state)["manual_charging"]["result"] == "works"
        state = apply(state, "说错了，我没有手动放回充电座", "m2", now=2)["state"]
        assert "manual_charging" not in by_capability(state)
        assert any(
            row["kind"] == "retract_observation" and row["capability"] == "manual_charging"
            for row in state["cases"][0]["observation_audit"]
        )
        # Reload does not resurrect the retracted fact.
        reloaded = support.load_state(json.loads(json.dumps(state)))
        assert "manual_charging" not in by_capability(reloaded)

    def test_new_negative_observation_supersedes_old_positive(self):
        state = apply(
            support.empty_state(),
            "自动回充失败，我手动放回充电座能充电",
            "m1",
            now=1,
        )["state"]
        state = apply(state, "说错了，手动放回其实充不上", "m2", now=2)["state"]
        assert by_capability(state)["manual_charging"]["result"] == "fails"
        captured = state["cases"][0]
        assert any(
            row["kind"] == "supersede_observation"
            and row["capability"] == "manual_charging"
            for row in captured["observation_audit"]
        )
        assert any(
            row["capability"] == "manual_charging" and row["fact_status"] == "superseded"
            for row in captured["observations"]
        )

    def test_same_message_is_idempotent(self):
        state = apply(support.empty_state(), S1, "m1", now=1)["state"]
        before = json.dumps(state["cases"][0]["observations"], ensure_ascii=False, sort_keys=True)
        audit_before = json.dumps(state["cases"][0]["observation_audit"], ensure_ascii=False, sort_keys=True)
        result = apply(state, S1, "m1", now=2)
        assert result["changed"] is False
        assert result["scope"] == "duplicate"
        after = json.dumps(result["state"]["cases"][0]["observations"], ensure_ascii=False, sort_keys=True)
        audit_after = json.dumps(result["state"]["cases"][0]["observation_audit"], ensure_ascii=False, sort_keys=True)
        assert before == after
        assert audit_before == audit_after


class TestCompatibilityAndPrompt:
    def test_old_v1_and_v2_load_add_observation_defaults(self):
        state = apply(support.empty_state(), "回充失败，我已经擦了充电触点", "m1", now=1)["state"]
        tried = state["cases"][0]["tried_actions"]
        for version in (1, 2):
            raw = copy.deepcopy(state)
            raw["schema_version"] = version
            for row in raw["cases"]:
                row.pop("observations", None)
                row.pop("observation_audit", None)
                row.pop("last_observation_result", None)
            loaded = support.load_state(json.loads(json.dumps(raw)))
            assert loaded["cases"][0]["observations"] == []
            assert loaded["cases"][0]["observation_audit"] == []
            assert loaded["cases"][0]["tried_actions"] == tried

    def test_snapshot_and_prompt_expose_scope(self):
        state = apply(support.empty_state(), S1, "m1", now=1)["state"]
        snapshot = support.decision_snapshot(state)
        assert snapshot["diagnostic_scope"]["task_goal"] == "automatic_return"
        assert snapshot["diagnostic_scope"]["resolved"] is False
        assert snapshot["confirmed_facts"]["observations"]
        prompt = support.prompt_context(state, snapshot)
        assert "【诊断范围｜观察 -> 能力范围 -> 诊断假设 -> 全局结案】" in prompt
        assert "手动放回充电：成功" in prompt
        assert "自动回充：失败" in prompt
        assert "root cause：unconfirmed" in prompt
        assert "仍未验证" in prompt
        assert "不能据此断言自动回充恢复" in prompt

    def test_semantic_quote_not_in_current_turn_is_rejected(self):
        state = apply(support.empty_state(), "自动回充失败。", "m1", now=1)["state"]
        payload = semantic(
            capability_observations=[
                {
                    "capability": "automatic_return",
                    "result": "works",
                    "evidence_quote": "它已经能自己回充了",  # not in this turn
                    "source": "user_message",
                }
            ]
        )
        result = apply(state, "还是不行。", "m2", now=2, interpretation=payload)
        active = case(result["state"])
        assert active["status"] == "active"
        assert active["diagnostic_scope"]["resolved"] is False
        errors = result["state"]["last_interpretation"]["trace"]["validation_errors"]
        assert any("quote_not_in_current_message" in error for error in errors)


def test_derive_does_not_mutate_case():
    state = apply(support.empty_state(), S1, "m1", now=1)["state"]
    active = state["cases"][0]
    before = copy.deepcopy(active)
    first = scope.derive(active)
    second = scope.derive(active)
    assert first == second
    assert active["observations"] == before["observations"]


class TestCanonicalAndIntegration:
    def test_normalize_observation_is_idempotent_and_accepts_quote_alias(self):
        text = "自动回充失败，但手动放回能充电。"
        payload = {
            "topic": "recharge_failure",
            "capability_observations": [
                {
                    "capability": "manual_charging",
                    "result": "works",
                    "evidence_quote": "手动放回能充电",
                    "source": "user_message",
                }
            ],
        }
        first, errors = interp.normalize_interpretation(payload, text)
        assert errors == []
        [observation] = first["capability_observations"]
        assert observation["evidence_quote"] == "手动放回能充电"
        assert observation["quote"] == "手动放回能充电"
        second, errors_two = interp.normalize_interpretation(first, text)
        assert errors_two == []
        assert second["capability_observations"] == first["capability_observations"]
        alias = {
            "topic": "recharge_failure",
            "capability_observations": [
                {
                    "capability": "manual_charging",
                    "result": "works",
                    "quote": "手动放回能充电",
                    "source": "user_message",
                }
            ],
        }
        third, errors_three = interp.normalize_interpretation(alias, text)
        assert errors_three == []
        assert third["capability_observations"] == first["capability_observations"]

    def test_conflicting_quote_fields_are_rejected(self):
        text = "自动回充失败，但手动放回能充电。"
        payload = {
            "topic": "recharge_failure",
            "capability_observations": [
                {
                    "capability": "manual_charging",
                    "result": "works",
                    "evidence_quote": "手动放回能充电",
                    "quote": "自动回充失败",
                    "source": "user_message",
                }
            ],
        }
        normalized, errors = interp.normalize_interpretation(payload, text)
        assert any("conflicting_evidence_quote_and_quote" in error for error in errors)
        assert normalized["capability_observations"] == []

    def test_retraction_normalization_is_idempotent(self):
        text = "自动回充失败，但手动放回能充电。说错了，我没有手动放回充电座。"
        payload = {
            "topic": "recharge_failure",
            "observation_retractions": [
                {
                    "capability": "manual_charging",
                    "evidence_quote": "我没有手动放回充电座",
                }
            ],
        }
        first, errors = interp.normalize_interpretation(payload, text)
        assert errors == []
        [retraction] = first["observation_retractions"]
        assert retraction["evidence_quote"] == "我没有手动放回充电座"
        second, errors_two = interp.normalize_interpretation(first, text)
        assert errors_two == []
        assert second["observation_retractions"] == first["observation_retractions"]
        conflict = {
            "topic": "recharge_failure",
            "observation_retractions": [
                {
                    "capability": "manual_charging",
                    "evidence_quote": "我没有手动放回充电座",
                    "quote": "说错了",
                }
            ],
        }
        normalized, errors_three = interp.normalize_interpretation(conflict, text)
        assert any("conflicting_evidence_quote_and_quote" in error for error in errors_three)
        assert normalized["observation_retractions"] == []

    def test_interpret_turn_to_state_accepts_real_semantic_observation(self):
        text = "自动回充失败，但手动放回能充电。"
        payload = {
            "topic": "recharge_failure",
            "capability_observations": [
                {
                    "capability": "manual_charging",
                    "result": "works",
                    "evidence_quote": "手动放回能充电",
                    "source": "user_message",
                }
            ],
            "closure_signal": "partial_success",
        }

        def fake(text_value, context=None):
            return {"payload": payload, "model": "fake-interpreter", "usage": {}}

        bundle = interp.interpret_turn(text, interpreter=fake)
        assert bundle["trace"]["method"] == "llm_structured"
        assert not bundle["trace"]["validation_errors"]
        result = support.apply_user_turn(
            support.empty_state(), text, "m1", now=1, interpretation=bundle
        )
        observations = {
            row["capability"]: row["result"]
            for row in case(result["state"])["observations"]
        }
        assert observations["manual_charging"] == "works"
        assert result["state"]["last_interpretation"]["trace"]["validation_errors"] == []

    def test_interpret_turn_rejects_hypothetical_and_foreign_quote(self):
        text = "自动回充失败，如果能充是不是说明触点没问题？"
        hypothetical = {
            "topic": "recharge_failure",
            "capability_observations": [
                {
                    "capability": "manual_charging",
                    "result": "works",
                    "evidence_quote": "如果能充是不是说明触点没问题",
                    "source": "user_message",
                }
            ],
            "closure_signal": "explicit_success",
        }

        def fake(text_value, context=None):
            return {"payload": hypothetical, "model": "fake-interpreter", "usage": {}}

        bundle = interp.interpret_turn(text, interpreter=fake)
        assert bundle["trace"]["validation_errors"]
        result = support.apply_user_turn(
            support.empty_state(), text, "m1", now=1, interpretation=bundle
        )
        assert "manual_charging" not in {
            row["capability"] for row in case(result["state"])["observations"]
        }
        assert case(result["state"])["status"] == "active"

        foreign = {
            "topic": "recharge_failure",
            "capability_observations": [
                {
                    "capability": "automatic_return",
                    "result": "works",
                    "evidence_quote": "它已经能自己回充了",
                    "source": "user_message",
                }
            ],
        }

        def fake_foreign(text_value, context=None):
            return {"payload": foreign, "model": "fake-interpreter", "usage": {}}

        other = "自动回充失败。"
        bundle_foreign = interp.interpret_turn(other, interpreter=fake_foreign)
        assert any(
            "quote_not_in_current_message" in error
            for error in bundle_foreign["trace"]["validation_errors"]
        )
        result_foreign = support.apply_user_turn(
            support.empty_state(), other, "m2", now=2, interpretation=bundle_foreign
        )
        foreign_observations = [
            row for row in case(result_foreign["state"])["observations"]
            if row["capability"] == "automatic_return"
        ]
        assert all(row["result"] != "works" for row in foreign_observations)

    def test_observation_and_audit_are_not_truncated_across_turns(self):
        state = apply(support.empty_state(), "自动回充失败。", "m1", now=1)["state"]
        last_text = ""
        for index in range(1, 46):
            last_text = f"手动放回充电座能充电（第{index}次）"
            state = apply(state, last_text, f"m{index + 1}", now=index + 1)["state"]
        active = state["cases"][0]
        observation_count = len(active["observations"])
        audit_count = len(active["observation_audit"])
        assert observation_count >= 45
        assert audit_count > 80
        reloaded = support.load_state(json.loads(json.dumps(state)))
        assert len(reloaded["cases"][0]["observations"]) == observation_count
        assert len(reloaded["cases"][0]["observation_audit"]) == audit_count
        duplicate = apply(reloaded, last_text, "m46", now=99)
        assert duplicate["changed"] is False
        assert len(duplicate["state"]["cases"][0]["observations"]) == observation_count
        assert len(duplicate["state"]["cases"][0]["observation_audit"]) == audit_count


class TestNextObservationConsistency:
    def test_manual_positive_auto_unknown_points_to_auto(self):
        state = apply(support.empty_state(), S2, "m1", now=1)["state"]
        active = case(state)
        assert active["next_step"]["kind"] == "observe"
        assert active["next_step"]["capability"] == "automatic_return"
        slots = {
            row["slot"]: row["status"] for row in state["cases"][0]["questions"]
        }
        assert slots.get("capability_observation:automatic_return") == "pending"
        assert not any(
            slot.startswith("step_outcome:") and status == "pending"
            for slot, status in slots.items()
        )
        assert "automatic_return" in active["diagnostic_scope"]["unverified_capabilities"]

    def test_both_capabilities_fail_points_to_supply_not_manual_repeat(self):
        state = apply(support.empty_state(), S3, "m1", now=1)["state"]
        active = case(state)
        assert active["next_step"]["kind"] == "observe"
        assert active["next_step"]["capability"] == "dock_power"
        assert all(
            "正常充上电" not in (row.get("question_text") or "")
            for row in state["cases"][0]["questions"]
            if row.get("status") == "pending"
        )
        slots = {
            row["slot"]: row["status"] for row in state["cases"][0]["questions"]
        }
        assert slots.get("capability_observation:dock_power") == "pending"

    def test_resolved_case_has_no_forced_still_needed(self):
        state = apply(support.empty_state(), "自动回充失败，回不到充电座。", "m1", now=1)["state"]
        state = apply(state, "现在它能从房间自己找到充电座，并且已经开始充电了。", "m2", now=2)["state"]
        active = case(state)
        assert active["status"] == "resolved"
        assert active["diagnostic_scope"]["still_needed"] == []
        assert active["next_step"] is None

    def test_retest_after_new_intervention_has_reason(self):
        state = apply(support.empty_state(), "自动回充失败。", "m1", now=1)["state"]
        state = apply(state, "我刚清理了充电触点，还没试能不能充。", "m2", now=2)["state"]
        active = case(state)
        # With no prior capability observation, the generic step-outcome confirm
        # is still the honest retest; it must carry the intervention reason.
        assert active["next_step"]["kind"] == "confirm"
        assert active["next_step"]["reason"] == "after_new_intervention"
        assert "你已做过" in active["next_step"]["detail"]
        slots = {
            row["slot"]: row["status"] for row in state["cases"][0]["questions"]
        }
        assert slots.get("step_outcome:clean_contacts") == "pending"

