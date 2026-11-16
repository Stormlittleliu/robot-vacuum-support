# -*- coding: utf-8 -*-
"""Scoring contract for the shared runtime action catalogue.

The scorer must not keep a second hard-coded action list: ``replace_dock`` and
``move_dock`` are observation-only (``auto_next=False``) in the runtime, but a
case that records the user doing them is still evaluable.  Unknown ids remain
``unsupported`` and other-domain state stays ``not_applicable`` (never a pass).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
for path in (ROOT, RUNTIME):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from eval import scorers  # noqa: E402
from robot_vacuum_support.actions import ACTION_CATALOG, ACTION_IDS  # noqa: E402


def _view(*, tried: list[dict] | None = None, not_tried: list[dict] | None = None) -> dict:
    return {
        "active_case": {
            "status": "active",
            "tried_actions": tried or [],
            "not_tried_actions": not_tried or [],
        },
        "cases": [{"case_id": "c1"}],
    }


def _by_name(checks: list[dict]) -> dict[str, dict]:
    return {check["name"]: check for check in checks}


def _catalog() -> dict[str, dict]:
    return {action["id"]: action for action in ACTION_CATALOG}


def test_scorer_catalog_is_the_shared_runtime_catalog():
    assert scorers._RECHARGE_ACTION_IDS
    assert set(scorers._RECHARGE_ACTION_IDS) == set(ACTION_IDS)


def test_replace_and_move_dock_are_evaluable_observation_actions():
    catalog = _catalog()
    assert catalog["replace_dock"]["auto_next"] is False
    assert catalog["move_dock"]["auto_next"] is False
    assert catalog["reseat_dock"]["auto_next"] is True

    checks = _by_name(
        scorers.score_state_subset(
            {
                "scope": "recharge",
                "tried_actions": [
                    {"action_id": "replace_dock", "outcome": "user_executed"},
                    {"action_id": "move_dock", "outcome": "user_executed"},
                ],
                "not_tried_actions": ["move_dock"],
            },
            _view(
                tried=[
                    {"action_id": "replace_dock", "outcome": "user_executed"},
                    {"action_id": "move_dock", "outcome": "user_executed"},
                ],
                not_tried=[{"action_id": "move_dock", "outcome": None}],
            ),
            scope="recharge",
        )
    )
    assert checks["state.tried_actions"]["status"] == "pass"
    assert checks["state.not_tried_actions"]["status"] == "pass"


def test_replace_dock_mismatch_is_a_real_fail_not_unsupported():
    checks = _by_name(
        scorers.score_state_subset(
            {
                "scope": "recharge",
                "tried_actions": [{"action_id": "replace_dock", "outcome": "user_executed"}],
            },
            _view(),
            scope="recharge",
        )
    )
    assert checks["state.tried_actions"]["status"] == "fail"


def test_unknown_action_remains_unsupported():
    checks = _by_name(
        scorers.score_state_subset(
            {
                "scope": "recharge",
                "tried_actions": [{"action_id": "clean_filter", "outcome": "failed"}],
                "not_tried_actions": [{"action_id": "replace_filter"}],
            },
            _view(),
            scope="recharge",
        )
    )
    assert checks["state.tried_actions"]["status"] == "unsupported"
    assert checks["state.not_tried_actions"]["status"] == "unsupported"


def test_other_domain_is_not_applicable_and_never_a_pass():
    checks = scorers.score_state_subset(
        {
            "scope": "suction",
            "case_status": "active",
            "tried_actions": [{"action_id": "replace_dock"}],
            "not_tried_actions": ["move_dock"],
        },
        _view(),
        scope="other",
    )
    assert checks
    assert {check["status"] for check in checks} == {"not_applicable"}
    assert not any(check["status"] == "pass" for check in checks)


def test_existing_reseat_and_outcome_semantics_are_unchanged():
    checks = _by_name(
        scorers.score_state_subset(
            {
                "scope": "recharge_failure",
                "tried_actions": [{"action_id": "reseat_dock", "outcome": "user_executed"}],
            },
            _view(tried=[{"action_id": "reseat_dock", "outcome": "user_executed"}]),
            scope="recharge",
        )
    )
    assert checks["state.tried_actions"]["status"] == "pass"
    assert checks["state.scope"]["status"] == "pass"

    empty = _by_name(
        scorers.score_state_subset(
            {"scope": "recharge", "tried_actions": []},
            _view(),
            scope="recharge",
        )
    )
    assert empty["state.tried_actions"]["status"] == "pass"


def test_importing_scorer_does_not_load_model_or_rag_stack(tmp_path):
    code = (
        "import sys\n"
        "from eval import scorers\n"
        "heavy = sorted(m for m in sys.modules if m == 'modules' or m.startswith('modules.'))\n"
        "assert not heavy, heavy\n"
        "assert 'replace_dock' in scorers._RECHARGE_ACTION_IDS\n"
        "assert 'move_dock' in scorers._RECHARGE_ACTION_IDS\n"
        "print('ok')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
