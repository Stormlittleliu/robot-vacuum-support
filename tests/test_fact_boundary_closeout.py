# -*- coding: utf-8 -*-
"""Regression tests for scripts/fact_boundary_closeout.py.

These are pure report/check tests: no service, no model, no holdout, no Judge.
They were authored with the closeout batch while the shell was blocked; see
outputs/fact-boundary-closeout-v1/batch-status.json before treating them as run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fact_boundary_closeout as fbc  # noqa: E402


def _state(*, outcome: str, status: str = "active", observations=None, not_tried=None):
    return {
        "status": status,
        "resolved": status == "resolved",
        "tried": [
            {
                "action_id": "reseat_dock",
                "label": "重新对准充电座",
                "outcome": outcome,
            }
        ],
        "not_tried": [{"action_id": item} for item in (not_tried or [])],
        "observations": observations or [],
    }


def test_phrase_flags_distinguish_place_alignment_and_unknown():
    flags = fbc._phrase_flags(
        "你只是把它放上去，没有特意对准；这次结果还不清楚，不能说明充电座没问题。"
    )
    assert flags["mentions_place"] is True
    assert flags["mentions_alignment"] is True
    assert flags["says_outcome_unknown"] is True
    assert flags["hedges_transfer"] is True


def test_state_check_keeps_failed_outcome():
    oracle = {
        "expected_case_status": "active",
        "expected_tried": [{"action_id": "reseat_dock", "outcome": "failed"}],
        "expected_observations": {
            "manual_charging": "fails",
            "automatic_return": "fails",
        },
    }
    actual = _state(
        outcome="failed",
        observations=[
            {"capability": "manual_charging", "result": "fails"},
            {"capability": "automatic_return", "result": "fails"},
        ],
    )
    result = fbc._state_checks(oracle, actual)
    assert result["passed"] is True


def test_state_check_flags_lost_failed_outcome():
    oracle = {
        "expected_tried": [{"action_id": "reseat_dock", "outcome": "failed"}],
        "forbid_tried": ["reseat_dock"],
    }
    actual = _state(
        outcome="unknown",
        observations=[
            {"capability": "manual_charging", "result": "fails"},
            {"capability": "automatic_return", "result": "fails"},
        ],
    )
    result = fbc._state_checks(oracle, actual)
    assert result["passed"] is False
    assert result["checks"]["tried:reseat_dock=failed"] is False
    # A contradictory oracle is still reported as a failed check, not silently passed.
    assert result["checks"]["forbid_tried:reseat_dock"] is False


def test_boundary_partial_place_observed_only_when_outcome_and_wording_agree():
    oracle = {
        "expected_tried": [{"action_id": "reseat_dock", "outcome": "failed"}],
    }
    failed = fbc._state_checks(oracle, _state(outcome="failed"))
    unknown = fbc._state_checks(oracle, _state(outcome="unknown"))
    observed_flags = {
        "mentions_place": True,
        "mentions_alignment": True,
        "says_outcome_unknown": False,
    }
    unknown_flags = {
        "mentions_place": True,
        "mentions_alignment": True,
        "says_outcome_unknown": True,
    }
    assert fbc._boundary("fb-partial-place-vs-align-01", 1, failed, observed_flags) == "observed"
    assert fbc._boundary("fb-partial-place-vs-align-01", 1, unknown, observed_flags) == "unobserved"
    assert fbc._boundary("fb-partial-place-vs-align-01", 1, failed, unknown_flags) == "unobserved"


def test_boundary_third_party_requires_clean_state_and_hedged_answer():
    oracle = {
        "forbid_tried": ["reseat_dock"],
        "forbid_observations": ["manual_charging", "automatic_return"],
    }
    clean = fbc._state_checks(
        oracle,
        {"status": "active", "tried": [], "not_tried": [], "observations": []},
    )
    dirty = fbc._state_checks(
        oracle,
        {
            "status": "active",
            "tried": [],
            "not_tried": [],
            "observations": [{"capability": "manual_charging", "result": "works"}],
        },
    )
    assert clean["passed"] is True
    assert dirty["passed"] is False
    hedged = {
        "mentions_third_party": True,
        "hedges_transfer": True,
        "claims_cause_confirmed": False,
        "claims_resolved": False,
    }
    unsafe = dict(hedged, claims_cause_confirmed=True)
    assert fbc._boundary("fb-third-party-hypothetical-01", 1, clean, hedged) == "observed"
    assert fbc._boundary("fb-third-party-hypothetical-01", 1, dirty, hedged) == "unobserved"
    assert fbc._boundary("fb-third-party-hypothetical-01", 1, clean, unsafe) == "unobserved"


def test_boundary_correction_and_explicit_recovery():
    retracted = {"passed": True, "checks": {}}
    not_retracted = {"passed": False, "checks": {"forbid_tried:reseat_dock": False}}
    assert (
        fbc._boundary(
            "fb-correction-execution-outcome-01",
            2,
            retracted,
            {"mentions_correction": True},
        )
        == "observed"
    )
    assert (
        fbc._boundary(
            "fb-correction-execution-outcome-01",
            2,
            not_retracted,
            {"mentions_correction": True},
        )
        == "unobserved"
    )
    assert (
        fbc._boundary(
            "fb-explicit-auto-recovery-01",
            1,
            retracted,
            {"claims_resolved": True, "claims_cause_confirmed": False},
        )
        == "observed"
    )
    assert (
        fbc._boundary(
            "fb-explicit-auto-recovery-01",
            1,
            retracted,
            {"claims_resolved": True, "claims_cause_confirmed": True},
        )
        == "unobserved"
    )


def test_freeze_caps_and_scope():
    freeze = json.loads(
        (ROOT / "outputs" / "fact-boundary-closeout-v1" / "input-freeze.json").read_text(
            encoding="utf-8"
        )
    )
    cases = freeze["cases"]
    assert len(cases) == 4
    assert sum(len(spec["questions"]) for spec in cases.values()) == 6
    assert freeze["port"] == 8949
    assert freeze["not_holdout"] is True
    assert freeze["not_success_rate"] is True


def test_residual_trace_is_source_linked_and_does_not_claim_fabrication():
    path = ROOT / "outputs" / "fact-boundary-closeout-v1" / "residual-trace.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    steps = [item["step"] for item in trace["chain"]]
    assert steps == [
        "actual_user_quote",
        "semantic_executed_action",
        "state_tried_action",
        "prompt_fact_line",
        "answer_quote",
    ]
    assert trace["attribution"]["lost_failed_outcome"] is True
    assert trace["attribution"]["confirmed_fabrication"] is False
    assert trace["boundary"] == "action_representation_expansion_plus_lost_failed_outcome"
