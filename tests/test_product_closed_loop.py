# -*- coding: utf-8 -*-
"""Focused regressions for the bounded product closed-loop slice (2026-11-15).

These cover only the confirmed failures and their counterexamples:
``pi-reseat-outcome-lost``, ``pi-third-party-fallback-observation``,
``pi-recovery-action-label-overreach``, and the read-only handoff projection.
No model, no holdout, no whole-suite run.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "runtime", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from robot_vacuum_support import actions, state as support  # noqa: E402


def apply(state, text, message_id, *, now=1, interpretation=None):
    return support.apply_user_turn(
        state, text, message_id, now=now, interpretation=interpretation
    )


def active(state):
    return support.public_view(state)["active_case"]


def observations(state):
    return {
        row["capability"]: row["result"]
        for row in (active(state) or {}).get("observations") or []
    }


def tried(state):
    return [
        (row["action_id"], row["outcome"])
        for row in (active(state) or {}).get("tried_actions") or []
    ]


# ---------------------------------------------------------------------------
# pi-reseat-outcome-lost
# ---------------------------------------------------------------------------
def test_validated_semantic_failed_outcome_is_not_overwritten_by_regex():
    text = (
        "我没有特意对准，只是把机器人随手放上充电座，结果还是充不上电。"
        "这样算试过重新对准吗？"
    )
    proposal = {
        "executed_actions": [
            {
                "action_id": "reseat_dock",
                "outcome": "failed",
                "evidence_quote": "结果还是充不上电",
                "source": "user_message",
            }
        ]
    }
    result = apply(support.empty_state(), text, "m-1", interpretation=proposal)
    assert ("reseat_dock", "failed") in tried(result["state"])
    row = next(
        item
        for item in active(result["state"])["tried_actions"]
        if item["action_id"] == "reseat_dock"
    )
    assert row["evidence"]
    assert row["source_message_id"] == "m-1"


def test_regex_fallback_still_preserves_failed_without_semantic_outcome():
    # Counterexample: a later explicit alignment phrase still has a failed
    # outcome after it; the fallback must not regress to unknown.
    text = "这次我特意把它对准充电极片放回充电座了，还是充不上电。"
    result = apply(support.empty_state(), text, "m-1")
    assert ("reseat_dock", "failed") in tried(result["state"])


# ---------------------------------------------------------------------------
# pi-third-party-fallback-observation
# ---------------------------------------------------------------------------
def test_third_party_hypothetical_is_not_a_current_user_observation_or_partial_close():
    text = (
        "如果朋友家的机器人也是自动回充失败，手动放上去能充，"
        "是不是就说明我家的充电座没问题？"
    )
    result = apply(support.empty_state(), text, "m-1", interpretation={"executed_actions": []})
    assert observations(result["state"]) == {}
    assert tried(result["state"]) == []
    assert (active(result["state"]).get("closure_evidence") or {}).get("level") != "partial_success"


def test_semantic_observation_inside_inherited_third_party_scope_is_rejected():
    text = (
        "如果朋友家的机器人也是自动回充失败，手动放上去能充，"
        "是不是就说明我家的充电座没问题？"
    )
    proposal = {
        "capability_observations": [
            {
                "capability": "manual_charging",
                "result": "works",
                "evidence_quote": "手动放上去能充",
                "source": "user_message",
            }
        ]
    }
    result = apply(support.empty_state(), text, "m-1", interpretation=proposal)
    assert observations(result["state"]) == {}
    errors = result["state"]["last_interpretation"]["trace"]["validation_errors"]
    assert any("source_scope_not_current_user" in error for error in errors)


def test_current_user_manual_observation_is_still_recorded():
    # Counterexample: the same manual-result wording is kept when the clause is
    # the user's own, not a friend's hypothetical.
    text = "我把它放回充电座，充上电了，但自动回充还是不行。"
    result = apply(support.empty_state(), text, "m-1")
    assert observations(result["state"]).get("manual_charging") == "works"
    assert observations(result["state"]).get("automatic_return") == "fails"


# ---------------------------------------------------------------------------
# pi-recovery-action-label-overreach
# ---------------------------------------------------------------------------
def test_automatic_recovery_cannot_become_checked_settings():
    text = "刚才我让机器人自己回充，它已经自己找到充电座并开始充电了。"
    proposal = {
        "closure_signal": "explicit_success",
        "executed_actions": [
            {
                "action_id": "check_auto_return",
                "outcome": "success",
                "evidence_quote": "自己回充",
                "source": "user_message",
            }
        ],
        "capability_observations": [
            {
                "capability": "automatic_return",
                "result": "works",
                "evidence_quote": "自己找到充电座并开始充电",
                "source": "user_message",
            }
        ],
    }
    result = apply(support.empty_state(), text, "m-1", interpretation=proposal)
    view = active(result["state"])
    assert view["status"] == "resolved"
    assert tried(result["state"]) == []
    assert observations(result["state"]).get("automatic_return") == "works"
    assert "检查自动回充设置" not in support.handoff_summary(result["state"], "s-1")["summary"]
    errors = result["state"]["last_interpretation"]["trace"]["validation_errors"]
    assert any("auto_return_settings_not_evidenced" in error for error in errors)


def test_real_settings_claim_is_still_allowed():
    state = apply(support.empty_state(), "回充失败", "m-1")["state"]
    text = "我检查了 APP 里的自动回充设置，没有被关闭。"
    result = apply(state, text, "m-2", now=2)
    assert ("check_auto_return", "unknown") in tried(result["state"])


def test_shared_reseat_dock_label_is_neutral_and_keeps_raw_quote():
    text = (
        "我没有特意对准，只是把机器人随手放上充电座，结果还是充不上电。"
        "这样算试过重新对准吗？"
    )
    state = apply(
        support.empty_state(),
        text,
        "m-p",
        interpretation={
            "executed_actions": [
                {
                    "action_id": "reseat_dock",
                    "outcome": "failed",
                    "evidence_quote": "结果还是充不上电",
                    "source": "user_message",
                }
            ]
        },
    )["state"]
    row = active(state)["tried_actions"][0]
    assert row["action_id"] == "reseat_dock"
    assert row["label"] == "手动放回充电座"
    assert "随手放上" in (row.get("evidence") or "")
    prompt = support.prompt_context(state)
    summary = support.handoff_summary(state, "s-p")["summary"]
    for rendered in (prompt, summary):
        assert "手动放回充电座" in rendered
        assert "随手放上" in rendered
        assert "重新对准充电座" not in rendered

    # Old persisted rows keep the raw evidence but must not keep the stale
    # expanded label in any read projection.
    raw = {
        "schema_version": 2,
        "turn": 1,
        "cases": [
            {
                "case_id": "case-old-place",
                "topic": "recharge_failure",
                "issue": {"text": "随手放上但充不上电", "reported_text": text},
                "status": "active",
                "stage": "diagnose",
                "tried_actions": [
                    {
                        "action_id": "reseat_dock",
                        "label": "重新对准充电座",
                        "user_executed": True,
                        "outcome": "failed",
                        "evidence": "我没有特意对准，只是把机器人随手放上充电座，结果还是充不上电。",
                        "source_message_id": "m-old-place",
                        "source_turn": 1,
                        "fact_status": "effective",
                        "provenance": "user_message",
                    }
                ],
                "not_tried_actions": [],
                "suggested_actions": [],
            }
        ],
        "active_case_id": "case-old-place",
    }
    loaded = support.load_state(raw)
    assert active(loaded)["tried_actions"][0]["label"] == "手动放回充电座"
    assert "手动放回充电座" in support.prompt_context(loaded)
    assert "手动放回充电座" in support.handoff_summary(loaded, "s-old-place")["summary"]
    assert "重新对准充电座" not in support.handoff_summary(loaded, "s-old-place")["summary"]


def test_old_persisted_bad_label_renders_neutral_in_view_prompt_and_summary():
    raw = {
        "schema_version": 2,
        "turn": 1,
        "cases": [
            {
                "case_id": "case-old",
                "topic": "recharge_failure",
                "issue": {"text": "回充恢复", "reported_text": "刚才我让机器人自己回充，它已经自己找到充电座并开始充电了。"},
                "status": "resolved",
                "stage": "resolved",
                "tried_actions": [
                    {
                        "action_id": "check_auto_return",
                        "label": "检查自动回充设置",
                        "user_executed": True,
                        "outcome": "success",
                        "evidence": "刚才我让机器人自己回充，它已经自己找到充电座并开始充电了。",
                        "source_message_id": "m-old",
                        "source_turn": 1,
                        "fact_status": "effective",
                        "provenance": "user_message",
                    }
                ],
                "not_tried_actions": [],
                "suggested_actions": [],
            }
        ],
        "active_case_id": "case-old",
    }
    loaded = support.load_state(raw)
    label = active(loaded)["tried_actions"][0]["label"]
    assert label == "自动回充（用户原话）"
    prompt = support.prompt_context(loaded)
    assert "自动回充（用户原话）" in prompt
    assert "检查自动回充设置" not in prompt
    summary = support.handoff_summary(loaded, "s-old")["summary"]
    assert "自动回充（用户原话）" in summary
    assert "检查自动回充设置" not in summary


# ---------------------------------------------------------------------------
# Handoff projection
# ---------------------------------------------------------------------------
def _recharge_state():
    state = apply(
        support.empty_state(),
        "我没有特意对准，只是把机器人随手放上充电座，结果还是充不上电。",
        "m-user-1",
        interpretation={
            "executed_actions": [
                {
                    "action_id": "reseat_dock",
                    "outcome": "failed",
                    "evidence_quote": "结果还是充不上电",
                    "source": "user_message",
                }
            ],
            "capability_observations": [
                {
                    "capability": "manual_charging",
                    "result": "fails",
                    "evidence_quote": "把机器人随手放上充电座，结果还是充不上电",
                    "source": "user_message",
                }
            ],
        },
    )["state"]
    state = support.apply_assistant_turn(
        state,
        "先确认充电座供电并观察指示灯；不要把它当成已对准。",
        "m-assistant-1",
        turn=1,
        retrieval={
            "status": "ok",
            "results": [
                {
                    "source": "故障排除.txt",
                    "chunk_id": "chunk-1",
                    "snippet": "确认充电座是否通电、指示灯是否亮起。",
                }
            ],
        },
        now=2,
    )
    return state


def test_structured_handoff_has_facts_refs_suggestions_and_bound_sources():
    state = _recharge_state()
    payload = support.handoff_summary(state, "s-1", messages=[])
    summary = payload["summary"]
    assert payload["mode"] == "structured_case"
    assert "当前问题" in summary
    assert "手动放回充电座" in summary
    assert "用户第1轮" in summary and "m-user-1" in summary
    assert "用户原话" in summary and "结果还是充不上电" in summary
    assert "助手建议" in summary and "不是事实" in summary
    assert "故障排除.txt" in summary and "chunk-1" in summary
    assert "未发送给任何外部售后系统" in payload["notice"]


def test_handoff_does_not_list_sources_when_retrieval_is_not_bound():
    state = _recharge_state()
    case = state["cases"][0]
    case["last_retrieval"] = {
        "status": "not_retrieved",
        "results": [{"source": "不该出现的文件.txt", "chunk_id": "bad"}],
    }
    summary = support.handoff_summary(state, "s-1")["summary"]
    assert "不该出现的文件.txt" not in summary
    assert "not_retrieved" in summary
    assert "不把未绑定文件名写成来源" in summary


def test_handoff_is_deterministic_and_read_only():
    state = _recharge_state()
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    first = support.handoff_summary(state, "s-1")
    second = support.handoff_summary(state, "s-1")
    assert first["summary"] == second["summary"]
    assert json.dumps(state, ensure_ascii=False, sort_keys=True) == before


def test_non_recharge_session_gets_transcript_summary_without_fault_state():
    result = apply(support.empty_state(), "卧室用，扫地机器人工作噪音选多少比较合适？", "m-nr-1")
    assert result["scope"] == "other"
    state = support.apply_assistant_turn(
        result["state"],
        "工作噪音≤55dB 更适合卧室；地毯模式噪音会升高。",
        "m-nr-a1",
        turn=1,
        retrieval={
            "status": "ok",
            "results": [
                {
                    "source": "选购指南.txt",
                    "chunk_id": "noise-18",
                    "snippet": "工作噪音≤55dB更适合卧室使用。",
                }
            ],
        },
        now=2,
    )
    assert support.public_view(state)["active_case"] is None
    messages = [
        {
            "role": "user",
            "content": "卧室用，扫地机器人工作噪音选多少比较合适？",
            "message_id": "m-nr-1",
            "turn": 1,
        },
        {
            "role": "assistant",
            "content": "工作噪音≤55dB 更适合卧室；地毯模式噪音会升高。",
            "message_id": "m-nr-a1",
            "turn": 1,
        },
    ]
    payload = support.handoff_summary(state, "s-nr", messages)
    summary = payload["summary"]
    assert payload["mode"] == "transcript_only"
    assert "当前问题（用户原话）" in summary
    assert "卧室用" in summary and "≤55dB" in summary
    assert "没有形成回充故障案例" in summary
    assert "不推断" in summary
    assert "选购指南.txt" in summary
    assert support.public_view(state)["active_case"] is None


# ---------------------------------------------------------------------------
# Server handoff route: GET/POST are the same read-only projection
# ---------------------------------------------------------------------------
class FakeConfig:
    instance_id = "inst-closed-loop"
    artifact_id = "art-closed-loop"
    scenario = "robot_vacuum"
    version = 4
    name = "robot-vacuum closed-loop test"
    host = "127.0.0.1"
    port = 8949
    workflows = ["agent"]
    default_workflow = "agent"
    issues = []
    manifest = {}

    def __init__(self, root: Path):
        self.sessions_root = root / "sessions"
        self.storage_root = root


def test_handoff_get_post_are_read_only_and_ended_session_stays_viewable():
    from modules.instances.runtime.server import create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(FakeConfig(Path(tmp)))
        with TestClient(app) as client:
            store = app.state.store
            session_id = store.create(workflow="agent")["session_id"]
            state = apply(
                support.empty_state(),
                "我没有特意对准，只是把机器人随手放上充电座，结果还是充不上电。",
                "m-1",
                interpretation={
                    "executed_actions": [
                        {
                            "action_id": "reseat_dock",
                            "outcome": "failed",
                            "evidence_quote": "结果还是充不上电",
                            "source": "user_message",
                        }
                    ]
                },
            )["state"]
            store.save_support_state(session_id, state)
            store.append_message(session_id, "user", "我没有特意对准，只是把机器人随手放上充电座，结果还是充不上电。", turn=1)
            path = store.session_root(session_id) / "support_state.json"
            before = path.read_bytes()

            get_one = client.get(f"/api/sessions/{session_id}/handoff-summary")
            post_one = client.post(f"/api/sessions/{session_id}/handoff-summary", json={})
            assert get_one.status_code == 200
            assert post_one.status_code == 200
            assert get_one.json()["summary"] == post_one.json()["summary"]
            assert path.read_bytes() == before

            store.mark(session_id, "ended")
            ended = client.get(f"/api/sessions/{session_id}/handoff-summary")
            assert ended.status_code == 200
            assert path.read_bytes() == before
