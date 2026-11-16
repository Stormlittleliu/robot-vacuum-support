# -*- coding: utf-8 -*-
"""Development oracle tests for the action-state contract.

These are same-family/counterexample checks, not holdout and not a success rate.
They exercise the deterministic reducer only (no model calls, no server).
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from robot_vacuum_support import actions  # noqa: E402
from robot_vacuum_support import interpretation  # noqa: E402
from robot_vacuum_support import questions  # noqa: E402
from robot_vacuum_support import routing  # noqa: E402
from robot_vacuum_support import state as support  # noqa: E402


def play(turns, interpretation_by_turn=None):
    state = support.empty_state()
    result = None
    message_no = 0
    for index, (role, text) in enumerate(turns, start=1):
        message_no += 1
        if role == "user":
            result = support.apply_user_turn(
                state,
                text,
                f"m-{message_no}",
                now=100 + index,
                interpretation=(interpretation_by_turn or {}).get(index),
            )
            state = result["state"]
        else:
            support.apply_assistant_turn(
                state,
                text,
                f"a-{message_no}",
                turn=state.get("turn"),
                now=100 + index,
            )
    return state, result


def active(state):
    return support.public_view(state)["active_case"]


def tried(state):
    return [(row["action_id"], row["outcome"]) for row in active(state)["tried_actions"]]


def not_tried(state):
    return [row["action_id"] for row in active(state)["not_tried_actions"]]


class ActionIdentityTests(unittest.TestCase):
    def test_shared_catalogue_has_no_enum_drift(self):
        self.assertEqual(set(actions.ACTION_BY_ID), set(actions.ACTION_PATTERNS))
        self.assertEqual(actions.ACTION_IDS, interpretation.ACTION_IDS)
        self.assertEqual(set(actions.ACTION_BY_ID), set(support.ACTION_BY_ID))
        self.assertFalse(actions.ACTION_BY_ID["replace_dock"]["auto_next"])
        self.assertFalse(actions.ACTION_BY_ID["move_dock"]["auto_next"])

    def test_replace_move_reseat_are_distinct(self):
        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "我换了新的充电座，还是不行"),
            ]
        )
        self.assertEqual(tried(state), [("replace_dock", "failed")])

        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "我把旧充电座挪到客厅了，还是不行"),
            ]
        )
        self.assertEqual(tried(state), [("move_dock", "failed")])

        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "我把机器人放回充电座，还是不行"),
            ]
        )
        self.assertEqual(tried(state), [("reseat_dock", "failed")])

    def test_move_does_not_match_moving_house(self):
        state, _ = play([("user", "回充失败，我搬完家找不到充电座放哪了")])
        self.assertEqual(tried(state), [])
        self.assertEqual((active(state)["next_step"] or {}).get("action_id"), "place_dock")

    def test_replace_is_never_auto_suggested(self):
        state, _ = play([("user", "回充失败"), ("user", "没换新座，只清了障")])
        next_step = active(state)["next_step"] or {}
        self.assertNotEqual(
            (next_step.get("action_id"), next_step.get("kind")),
            ("replace_dock", "action"),
        )
        self.assertNotEqual(next_step.get("action_id"), "move_dock")


class ExecutionOutcomeTests(unittest.TestCase):
    def test_executed_unknown_does_not_resolve(self):
        state, _ = play([("user", "回充失败"), ("user", "我已经擦了触点")])
        self.assertEqual(tried(state), [("clean_contacts", "unknown")])
        self.assertEqual(active(state)["status"], "active")
        self.assertIsNone(active(state)["resolved_at"])

    def test_explicit_failure_after_action(self):
        state, _ = play([("user", "回充失败"), ("user", "擦了触点还是不行")])
        self.assertEqual(tried(state), [("clean_contacts", "failed")])
        self.assertEqual(active(state)["status"], "active")

    def test_partial_local_success_does_not_close_case(self):
        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "我把机器人手动放回充电座能充，但自动回充还是不行"),
            ]
        )
        case = active(state)
        self.assertEqual(case["status"], "active")
        self.assertIsNone(case["resolved_at"])
        self.assertIsNone(case["resolution"])
        self.assertEqual((case.get("closure_evidence") or {}).get("level"), "partial_success")

    def test_success_does_not_promote_old_suggestion_to_tried(self):
        state = support.empty_state()
        state = support.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
        state = support.apply_assistant_turn(
            state, "先清洁充电触点。", "a1", turn=1, now=2
        )
        state = support.apply_user_turn(state, "现在能正常回充了", "m2", now=3)["state"]
        self.assertEqual(active(state)["status"], "resolved")
        self.assertEqual(tried(state), [])

    def test_tried_question_does_not_swallow_execution_statement(self):
        state, _ = play(
            [("user", "回充失败，我刚才擦了充电触点，还没看结果，怎么办？")]
        )
        self.assertEqual(tried(state), [("clean_contacts", "unknown")])
        state = support.apply_user_turn(state, "我刚才试过什么？", "m2", now=2)["state"]
        self.assertIn("清洁充电触点", support.query_tried_answer(state))

    def test_non_execution_negation_does_not_become_not_tried(self):
        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "我擦了触点但是没擦干净"),
            ]
        )
        self.assertEqual(tried(state), [("clean_contacts", "unknown")])
        self.assertEqual(not_tried(state), [])


class NegationCorrectionTests(unittest.TestCase):
    def test_not_tried_then_executed_supersedes_old_negation(self):
        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "我没擦触点"),
                ("user", "擦了触点"),
            ]
        )
        self.assertEqual(tried(state), [("clean_contacts", "unknown")])
        self.assertEqual(not_tried(state), [])
        audits = active(state)["fact_audit"]
        self.assertTrue(
            any(a["kind"] == "supersede_not_tried" and a["action_id"] == "clean_contacts" for a in audits)
        )

    def test_correction_retracts_tried_and_derived_outcome(self):
        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "擦了触点还是不行"),
                ("user", "说错了，我没擦触点，只清了障"),
            ]
        )
        self.assertEqual(tried(state), [("clear_dock_obstacles", "unknown")])
        self.assertEqual(not_tried(state), ["clean_contacts"])
        audits = active(state)["fact_audit"]
        retract = next(a for a in audits if a["kind"] == "retract_tried")
        self.assertEqual(retract["action_id"], "clean_contacts")
        self.assertEqual(retract["message_id"], "m-3")
        self.assertEqual(retract["replaced_fact"]["outcome"], "failed")
        self.assertIn("没擦触点", retract["text"])

    def test_compound_correction_keeps_move_and_revokes_replace(self):
        for correction in ("说错了，没换新座，只挪了旧座", "说错了，不是换新座，只挪了旧座"):
            with self.subTest(correction=correction):
                state, _ = play(
                    [
                        ("user", "回充失败"),
                        ("user", "我换了新的充电座"),
                        ("user", correction),
                    ]
                )
                self.assertEqual(tried(state), [("move_dock", "unknown")])
                self.assertEqual(not_tried(state), ["replace_dock"])
                self.assertNotIn(
                    "reseat_dock", [action_id for action_id, _ in tried(state)]
                )

    def test_explicit_object_negation_does_not_touch_other_tried(self):
        state, _ = play([("user", "回充失败"), ("user", "擦了触点，也清理了充电座周围")])
        self.assertEqual(
            set(tried(state)),
            {("clean_contacts", "unknown"), ("clear_dock_obstacles", "unknown")},
        )
        state = support.apply_user_turn(state, "我没擦触点", "m3", now=3)["state"]
        self.assertEqual(tried(state), [("clear_dock_obstacles", "unknown")])
        self.assertEqual(not_tried(state), ["clean_contacts"])

    def test_same_action_never_effective_tried_and_not_tried(self):
        state, _ = play(
            [
                ("user", "回充失败"),
                ("user", "我没擦触点"),
                ("user", "擦了触点"),
                ("user", "说错了，我没擦触点"),
            ]
        )
        case = state["cases"][0]
        effective_tried = {
            row["action_id"]
            for row in case["tried_actions"]
            if row.get("fact_status", "effective") == "effective"
        }
        effective_not_tried = {
            row["action_id"]
            for row in case["not_tried_actions"]
            if row.get("fact_status", "effective") == "effective"
        }
        self.assertFalse(effective_tried & effective_not_tried)


class GeneralizedNegationTests(unittest.TestCase):
    def test_generalized_negation_binds_explicitly_asked_action(self):
        state = support.empty_state()
        state = support.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
        state = support.apply_assistant_turn(
            state, "先清理充电座周围障碍物。", "a1", turn=1, now=2
        )
        result = support.apply_user_turn(state, "都没试过", "m2", now=3)
        state = result["state"]
        self.assertIsNone(result["direct_answer"])
        self.assertEqual(not_tried(state), ["clear_dock_obstacles"])
        binding = state["cases"][0]["last_fact_binding"]
        self.assertEqual(binding["source"], "last_asked_actions")
        self.assertEqual(binding["turn"], 2)

    def test_generalized_negation_without_referent_asks_instead_of_guessing(self):
        state, _ = play([("user", "回充失败"), ("user", "都没试过")])
        self.assertEqual(not_tried(state), [])
        self.assertEqual(tried(state), [])
        self.assertIn("哪些", active(state)["clarification"])
        self.assertEqual(
            state["cases"][0]["negation_ambiguity"][-1]["status"], "needs_referent"
        )

    def test_generalized_negation_too_many_referents_records_ambiguity(self):
        state = support.empty_state()
        state = support.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
        case = state["cases"][0]
        case["suggested_actions"] = [
            {
                "action_id": action_id,
                "label": actions.ACTION_BY_ID[action_id]["label"],
                "primary": index == 0,
                "status": "suggested",
                "source_turn": 1,
            }
            for index, action_id in enumerate(
                ["clear_dock_obstacles", "clean_contacts", "clean_sensors", "check_power"]
            )
        ]
        result = support.apply_user_turn(state, "都没试过", "m2", now=2)
        state = result["state"]
        self.assertEqual(not_tried(state), [])
        self.assertIn("数量较多", result["direct_answer"] or "")
        self.assertEqual(
            state["cases"][0]["negation_ambiguity"][-1]["status"], "too_many"
        )

    def test_generalized_negation_conflict_with_success_records_ambiguity(self):
        state = support.empty_state()
        state = support.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
        case = state["cases"][0]
        case["tried_actions"].append(
            {
                "action_id": "clean_contacts",
                "label": "清洁充电触点",
                "user_executed": True,
                "outcome": "success",
                "source_message_id": "m0",
                "source_turn": 1,
                "fact_status": "effective",
            }
        )
        case["suggested_actions"] = [
            {
                "action_id": action_id,
                "label": actions.ACTION_BY_ID[action_id]["label"],
                "primary": index == 0,
                "status": "suggested",
                "source_turn": 1,
            }
            for index, action_id in enumerate(["clean_contacts", "check_power"])
        ]
        result = support.apply_user_turn(state, "都没试过", "m2", now=2)
        state = result["state"]
        self.assertEqual(tried(state), [("clean_contacts", "success")])
        self.assertIn("冲突", result["direct_answer"] or "")
        self.assertEqual(
            state["cases"][0]["negation_ambiguity"][-1]["status"], "conflicting_success"
        )


class RestartAndSemanticScopeTests(unittest.TestCase):
    def test_service_restart_is_not_reset_robot(self):
        state, _ = play(
            [("user", "回充失败"), ("user", "服务重启了，还是不行")]
        )
        self.assertNotIn("reset_robot", [action_id for action_id, _ in tried(state)])

    def test_explicit_robot_restart_is_recorded(self):
        state, _ = play(
            [("user", "回充失败"), ("user", "重启了机器人，还是不行")]
        )
        self.assertEqual(tried(state), [("reset_robot", "failed")])

    def test_semantic_quote_forgery_is_rejected(self):
        state, result = play(
            [("user", "回充失败")],
            interpretation_by_turn={
                1: {
                    "topic": "recharge_failure",
                    "executed_actions": [
                        {
                            "action_id": "clean_contacts",
                            "outcome": "failed",
                            "evidence_quote": "我擦了触点",
                        }
                    ],
                }
            },
        )
        self.assertEqual(tried(state), [])
        trace = state["last_interpretation"]["trace"]
        self.assertTrue(
            any("quote not in user message" in item for item in trace["validation_errors"])
        )

    def test_semantic_action_does_not_match_quote_family(self):
        state, _ = play(
            [("user", "回充失败，我换了新的充电座")],
            interpretation_by_turn={
                1: {
                    "topic": "recharge_failure",
                    "executed_actions": [
                        {
                            "action_id": "clean_contacts",
                            "outcome": "failed",
                            "evidence_quote": "我换了新的充电座",
                        }
                    ],
                }
            },
        )
        self.assertNotIn("clean_contacts", [action_id for action_id, _ in tried(state)])
        self.assertIn("replace_dock", [action_id for action_id, _ in tried(state)])
        trace = state["last_interpretation"]["trace"]
        self.assertTrue(
            any("quote_action_mismatch" in item for item in trace["validation_errors"])
        )

    def test_previous_turn_quote_is_rejected(self):
        state = support.empty_state()
        state = support.apply_user_turn(state, "回充失败", "m1", now=1)["state"]
        state = support.apply_user_turn(state, "擦了触点", "m2", now=2)["state"]
        result = support.apply_user_turn(
            state,
            "还是不行",
            "m3",
            now=3,
            interpretation={
                "topic": "recharge_failure",
                "executed_actions": [
                    {
                        "action_id": "clean_contacts",
                        "outcome": "failed",
                        "evidence_quote": "擦了触点",
                    }
                ],
            },
        )
        self.assertEqual(tried(result["state"]), [("clean_contacts", "unknown")])
        self.assertTrue(
            any(
                "quote not in user message" in item
                for item in result["state"]["last_interpretation"]["trace"]["validation_errors"]
            )
        )


class CompatibilityAndConsumersTests(unittest.TestCase):
    def _legacy_state(self, *, conflict=False):
        case = {
            "case_id": "case-legacy",
            "topic": "recharge_failure",
            "issue": {"text": "回充失败", "reported_text": "回充失败"},
            "status": "active",
            "stage": "diagnose",
            "tried_actions": [
                {
                    "action_id": "clean_contacts",
                    "label": "清洁充电触点",
                    "user_executed": True,
                    "outcome": "success",
                    "source_message_id": "old-1",
                    "source_turn": 1,
                }
            ],
            "not_tried_actions": [],
            "suggested_actions": [],
            "sources": [],
            "created_at": 1,
            "updated_at": 1,
        }
        if conflict:
            case["not_tried_actions"].append(
                {
                    "action_id": "clean_contacts",
                    "label": "清洁充电触点",
                    "source_message_id": "old-2",
                    "source_turn": 2,
                }
            )
        return {
            "schema_version": 1,
            "turn": 2,
            "active_case_id": "case-legacy",
            "cases": [case],
            "open_tasks": [],
        }

    def test_old_schema_success_is_preserved_not_reinterpreted(self):
        state = support.load_state(copy.deepcopy(self._legacy_state()))
        row = state["cases"][0]["tried_actions"][0]
        self.assertEqual(row["outcome"], "success")
        self.assertEqual(row["fact_status"], "effective")
        self.assertEqual(row["provenance"], "legacy_pre_contract_outcome_preserved")
        self.assertEqual(state["cases"][0]["fact_contract_origin"], "legacy_v1")

    def test_old_schema_conflict_reconciled_with_audit(self):
        state = support.load_state(copy.deepcopy(self._legacy_state(conflict=True)))
        case = state["cases"][0]
        tried_rows = [
            row for row in case["tried_actions"] if row.get("fact_status") == "effective"
        ]
        not_tried_rows = [
            row for row in case["not_tried_actions"] if row.get("fact_status") == "effective"
        ]
        self.assertEqual(tried_rows, [])
        self.assertEqual([row["action_id"] for row in not_tried_rows], ["clean_contacts"])
        self.assertTrue(case["legacy_fact_reconciliation"])
        self.assertTrue(any(a["reason"] == "legacy_conflict_later_not_tried" for a in case["fact_audit"]))

    def test_duplicate_message_does_not_duplicate_retraction(self):
        state, _ = play([("user", "回充失败"), ("user", "擦了触点还是不行")])
        result = support.apply_user_turn(
            state, "说错了，我没擦触点，只清了障", "m-3", now=300
        )
        state = result["state"]
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        audit_count = len(active(state)["fact_audit"])
        replay = support.apply_user_turn(
            state, "说错了，我没擦触点，只清了障", "m-3", now=400
        )
        self.assertFalse(replay["changed"])
        self.assertEqual(
            json.dumps(replay["state"], ensure_ascii=False, sort_keys=True), before
        )
        self.assertEqual(len(active(replay["state"])["fact_audit"]), audit_count)

    def test_authoritative_facts_are_shared_by_snapshot_prompt_query_routing(self):
        state, _ = play([("user", "回充失败"), ("user", "擦了触点还是不行")])
        version_before = routing.state_version(state)
        state = support.apply_user_turn(
            state, "说错了，我没擦触点，只清了障", "m-3", now=300
        )["state"]
        snapshot = support.decision_snapshot(state)
        confirmed = snapshot["confirmed_facts"]
        self.assertEqual(
            [(item["action_id"], item["outcome"]) for item in confirmed["tried"]],
            [("clear_dock_obstacles", "unknown")],
        )
        self.assertEqual(
            [item["action_id"] for item in confirmed["not_tried"]], ["clean_contacts"]
        )
        context = support.prompt_context(state, snapshot)
        self.assertNotIn("清洁充电触点：失败", context)
        self.assertIn("用户明确表示没试过", context)
        answer = support.query_tried_answer(state)
        self.assertIn("清理充电座周围障碍物", answer)
        self.assertIn("没做过：清洁充电触点", answer)
        self.assertNotEqual(version_before, routing.state_version(state))

    def test_question_lifecycle_invalidates_answer_from_retracted_fact(self):
        state, _ = play([("user", "回充失败"), ("user", "擦了触点还是不行")])
        case = state["cases"][0]
        questions.ensure_question(
            case,
            slot="step_outcome:clean_contacts",
            source="policy",
            question_text="清洁触点后恢复了吗？",
            turn=2,
            now=200,
        )
        questions.answer_question(
            case,
            "step_outcome:clean_contacts",
            turn=2,
            value="failed",
            reason="known_fact_satisfies_slot",
            now=200,
        )
        state = support.apply_user_turn(
            state, "说错了，我没擦触点，只清了障", "m-3", now=300
        )["state"]
        row = questions.find_question(state["cases"][0], "step_outcome:clean_contacts")
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(row["supersede_reason"], "source_tried_fact_retracted")


if __name__ == "__main__":
    unittest.main()
