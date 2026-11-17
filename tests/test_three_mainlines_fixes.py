# -*- coding: utf-8 -*-
"""Targeted auxiliary checks for the three-mainlines fixes.

These tests only protect the specific mechanisms changed in this round.  They
are not a substitute for the full-product HTTP/SSE acceptance run.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from core import paths as core_paths  # noqa: E402
from core.llm import errors as llm_errors  # noqa: E402
from core.observability.redact import redact_text  # noqa: E402
from robot_vacuum_support import state as support  # noqa: E402


def _model_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "modules.json").write_text("{}", encoding="utf-8")
    return path


def test_full_model_dir_binding_is_not_double_nested(tmp_path, monkeypatch):
    full = _model_dir(tmp_path / "bge-small-zh-v1.5")
    monkeypatch.setattr(core_paths, "MODEL_ROOT", full)
    assert core_paths.resolve_model_path("data/models/bge-small-zh-v1.5") == full
    assert core_paths.resolve_model_path("bge-small-zh-v1.5") == full


def test_legacy_parent_models_root_still_appends(tmp_path, monkeypatch):
    parent = tmp_path / "models"
    full = _model_dir(parent / "bge-small-zh-v1.5")
    monkeypatch.setattr(core_paths, "MODEL_ROOT", parent)
    assert core_paths.resolve_model_path("data/models/bge-small-zh-v1.5") == full
    assert core_paths.resolve_model_path("bge-small-zh-v1.5") == full


def test_auth_failure_is_typed_without_raw_provider_text():
    class FakeAuthError(Exception):
        status_code = 401

    failure = llm_errors.classify_model_exception(
        FakeAuthError("Error code: 401 - Your api key: sk-secret-value is invalid")
    )
    assert failure.code == "model_auth_failed"
    assert failure.requires_configuration is True
    assert "sk-secret" not in failure.user_message
    assert "sk-secret" not in llm_errors.safe_exception_summary(
        FakeAuthError("Error code: 401 - Your api key: sk-secret-value is invalid")
    )


def test_empty_answer_is_not_classified_as_401():
    failure = llm_errors.ModelOutputFailure("model_empty_answer")
    assert failure.code == "model_empty_answer"
    assert failure.code != "model_auth_failed"


def test_redaction_masks_keys_and_authorization():
    text = "Authorization: Bearer abcdefghijklmnop api_key=sk-supersecret123"
    redacted = redact_text(text)
    assert "abcdefghijklmnop" not in redacted
    assert "sk-supersecret123" not in redacted


def _tried(case):
    return [(row["action_id"], row["outcome"]) for row in case["tried_actions"]]


def _not_tried(case):
    return [row["action_id"] for row in case["not_tried_actions"]]


def test_correction_retracts_when_interpreter_falls_back():
    state = support.empty_state()
    state = support.apply_user_turn(
        state,
        "我已经把它放回充电座了，但还是充不上电。",
        "m1",
        now=1,
    )["state"]
    case = support.public_view(state)["active_case"]
    assert _tried(case) == [("reseat_dock", "failed")]

    state = support.apply_user_turn(
        state,
        "更正一下，刚才说错了：其实我没有把它放回充电座，只是按了回充键，自动回充也还没再试。",
        "m2",
        now=2,
    )["state"]
    case = support.public_view(state)["active_case"]
    assert _tried(case) == []
    assert set(_not_tried(case)) == {"reseat_dock", "check_auto_return"}


def test_result_negation_counterexample_keeps_tried_fact():
    state = support.empty_state()
    state = support.apply_user_turn(
        state,
        "我清理了充电触点，但还是充不上电。",
        "m1",
        now=1,
    )["state"]
    state = support.apply_user_turn(
        state,
        "更正一下，刚才说错了：不是没清理，是清理了但没清理干净触点。",
        "m2",
        now=2,
    )["state"]
    case = support.public_view(state)["active_case"]
    assert _tried(case) == [("clean_contacts", "failed")]
    assert _not_tried(case) == []
