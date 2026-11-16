# -*- coding: utf-8 -*-
"""Oracle corrections for the first v1 review pass (revision v1.1).

The P1 product only keeps structured state for the recharge-failure flow.
Other QA topics must not be forced into a fake state schema.  ``not_tried``
means the user explicitly said an action was not done; silence is unknown, not
"not tried".  These corrections are applied after seed expansion and recorded
in audit.json, so the correction is auditable rather than silent.
"""
from __future__ import annotations

from typing import Any

PREVIOUS_REVISION = "v1.0"
PREVIOUS_CASES_SHA256 = "e766dda85854da9f90ffa24889139b40669b8eec04baf5531015330b87a2bf4f"
PREVIOUS_HOLDOUT_SHA256 = "558918e7dca2c5854428b18c6add6315a30c585b79419a1e13099ea8124f269e"

# Cases that are answer/reference-only for the current P1.  All structured
# state assertions are removed; QA rubrics remain.
ANSWER_ONLY_CASES = {
    "rsv1-dev-direct-mop-no-water-01",
    "rsv1-dev-direct-mop-no-water-02",
    "rsv1-dev-direct-suction-decline-01",
    "rsv1-dev-direct-suction-decline-02",
    "rsv1-dev-negation-filter-not-cleaned-01",
    "rsv1-dev-negation-filter-not-cleaned-02",
    "rsv1-dev-correction-mop-water-level-01",
    "rsv1-dev-correction-mop-water-level-02",
    "rsv1-dev-ambiguity-recharge-condition-02",  # metric/purchase-condition QA
    "rsv1-dev-boundary-unknown-model-price-01",
    "rsv1-dev-boundary-unknown-model-price-02",
    "rsv1-dev-boundary-out-of-domain-01",
    "rsv1-dev-boundary-out-of-domain-02",
    "rsv1-dev-boundary-missing-context-01",
    "rsv1-dev-boundary-missing-context-02",
    "rsv1-hold-direct-charge-heat-01",
    "rsv1-hold-direct-charge-heat-02",
    "rsv1-hold-direct-edge-brush-wear-01",
    "rsv1-hold-direct-edge-brush-wear-02",
    "rsv1-hold-boundary-renovation-debris-01",
    "rsv1-hold-boundary-renovation-debris-02",
    "rsv1-hold-fault-empty-retrieval-01",  # mop fault; answer-only
}

# 1-based turn index -> explicit "user did not do this" list.  Nothing else is
# kept as not_tried.
EXPLICIT_NOT_TRIED: dict[str, dict[int, list[str]]] = {
    "rsv1-dev-multi-recharge-negation-01": {3: ["clear_dock_obstacles", "clean_contacts"]},
    "rsv1-dev-multi-recharge-negation-02": {3: ["clear_dock_obstacles"]},
    "rsv1-dev-multi-recharge-correction-01": {3: ["clean_contacts"]},
    "rsv1-dev-multi-recharge-correction-02": {3: ["replace_dock"]},
    "rsv1-dev-negation-filter-not-cleaned-01": {3: ["clean_filter"]},
    "rsv1-dev-negation-filter-not-cleaned-02": {1: ["clean_filter"], 3: ["clean_filter"]},
    "rsv1-hold-negation-battery-vs-base-01": {3: ["replace_battery"]},
    "rsv1-hold-negation-battery-vs-base-02": {3: ["replace_dock"]},
}

# Topic switches preserve the current recharge state; they do not create a
# mop/suction state.  Other overrides remove tried actions that were only
# assistant suggestions or observations not explicitly executed by the user.
STATE_OVERRIDES: dict[str, dict[int, dict[str, Any]]] = {
    "rsv1-dev-multi-topic-switch-mop-01": {
        2: {"case_status": "active", "scope": "recharge"},
    },
    "rsv1-dev-multi-topic-switch-mop-02": {
        2: {"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}]},
    },
    "rsv1-hold-multi-switch-to-suction-01": {
        2: {"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}]},
    },
    "rsv1-hold-multi-switch-to-suction-02": {
        2: {"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "check_power", "outcome": "success"}]},
    },
    "rsv1-dev-multi-recharge-closure-02": {
        3: {"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "reseat_dock", "outcome": "success"}]},
    },
    "rsv1-hold-multi-base-moved-remap-01": {
        2: {"case_status": "active", "scope": "recharge"},
    },
    "rsv1-hold-multi-docking-alignment-01": {
        2: {"case_status": "active", "scope": "recharge"},
    },
}


def apply_state_policy(cases: list[dict]) -> list[dict]:
    corrections: list[dict] = []
    for case in cases:
        cid = case["id"]
        if cid in ANSWER_ONLY_CASES:
            before = [dict(turn.get("expected", {}).get("state") or {}) for turn in case["turns"]]
            for turn in case["turns"]:
                turn["expected"]["state"] = {}
            case["state_scope"] = "planned_out_of_scope_current_p1"
            case["scoring"]["state_score"] = "not_applicable_current_p1"
            corrections.append({
                "case_id": cid,
                "kind": "answer_only_state_removed",
                "removed_states": before,
                "reason": "P1 only structures recharge-failure state; this case is another topic/safety/purchase/single-turn QA and must not force a fake state schema.",
            })
        else:
            case["state_scope"] = "current_p1_recharge_state"
            case["scoring"]["state_score"] = "included_current_p1"

        explicit = EXPLICIT_NOT_TRIED.get(cid, {})
        for index, turn in enumerate(case["turns"], 1):
            if turn.get("role") != "user":
                continue
            state = turn["expected"].get("state") or {}
            old = list(state.get("not_tried_actions") or [])
            keep = list(explicit.get(index, []))
            removed = [item for item in old if item not in keep]
            if removed:
                corrections.append({
                    "case_id": cid,
                    "turn": index,
                    "kind": "not_tried_removed_unless_explicit_negation",
                    "removed": removed,
                    "kept": keep,
                    "reason": "Only an explicit user negation creates not_tried; silence and assistant suggestions do not.",
                })
            if old or keep:
                if keep:
                    state["not_tried_actions"] = keep
                else:
                    state.pop("not_tried_actions", None)

        for index, replacement in STATE_OVERRIDES.get(cid, {}).items():
            turn = case["turns"][index - 1]
            old = dict(turn["expected"].get("state") or {})
            turn["expected"]["state"] = dict(replacement)
            corrections.append({
                "case_id": cid,
                "turn": index,
                "kind": "state_override",
                "old": old,
                "new": dict(replacement),
                "reason": "Preserve current recharge state on topic switch, or remove tried actions not explicitly executed by the user.",
            })
    return corrections
