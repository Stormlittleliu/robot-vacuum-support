# -*- coding: utf-8 -*-
"""16 starter development cases for the recharge-failure flow.

These are hand-written deterministic fixtures, not the 40-60 formal eval set
and not evidence of real-LLM success.  The independent case owner may publish
``eval/cases/v0/recharge-start.json``; this fixture keeps the runner usable
before/without that file.
"""
from __future__ import annotations

CASES = [
    {
        "id": "C01",
        "title": "首轮回充失败，建立案例并给出首个可试步骤",
        "turns": [("user", "回充失败怎么办")],
        "expect": {"scope": "recharge", "has_case": True, "status": "active", "tried": [], "next_action": "clear_dock_obstacles"},
    },
    {
        "id": "C02",
        "title": "换一种说法仍进入同一回充故障流程",
        "turns": [("user", "机器人充不上电")],
        "expect": {"has_case": True, "status": "active", "next_action": "clear_dock_obstacles"},
    },
    {
        "id": "C03",
        "title": "只从用户消息提取可选型号并保留来源轮次",
        "turns": [("user", "我家的是石头G20，充不上电")],
        "expect": {"model": "石头G20", "model_source_turn": 1},
    },
    {
        "id": "C04",
        "title": "否定“没清理”不能标为已试",
        "turns": [("user", "回充失败"), ("user", "我没清理触点，还是不行")],
        "expect": {"tried": [], "not_tried": ["clean_contacts"], "next_action": "clean_contacts"},
    },
    {
        "id": "C05",
        "title": "“擦了触点还是不行”记为失败并推进下一步",
        "turns": [("user", "充不上电"), ("user", "我已经擦了触点，还是不行")],
        "expect": {"tried": [("clean_contacts", "failed")], "next_action": "clear_dock_obstacles"},
    },
    {
        "id": "C06",
        "title": "“还是不行”关联唯一最近建议，而不是所有建议",
        "turns": [("user", "充不上电"), ("assistant", "建议先清理充电座周围障碍物。"), ("user", "还是不行")],
        "expect": {"tried": [("clear_dock_obstacles", "failed")], "next_action": "clean_contacts"},
    },
    {
        "id": "C07",
        "title": "“擦了触点，还是没有反应”关联明确动作",
        "turns": [("user", "充不上电"), ("user", "擦了触点，还是没有反应")],
        "expect": {"tried": [("clean_contacts", "failed")], "next_action": "clear_dock_obstacles"},
    },
    {
        "id": "C08",
        "title": "已做但未反馈结果时追问结果，不擅自判失败",
        "turns": [("user", "充不上电"), ("user", "擦了触点")],
        "expect": {"tried": [("clean_contacts", "unknown")], "next_kind": "confirm"},
    },
    {
        "id": "C09",
        "title": "“充上了”结案",
        "turns": [("user", "充不上电"), ("user", "充上了")],
        "expect": {"status": "resolved", "next_action": None},
    },
    {
        "id": "C10",
        "title": "“恢复了”结案，不继续推荐故障步骤",
        "turns": [("user", "充不上电"), ("user", "恢复了，谢谢")],
        "expect": {"status": "resolved", "next_action": None},
    },
    {
        "id": "C11",
        "title": "任意“不能”不误判为回充全失败",
        "turns": [("user", "充不上电"), ("user", "不能开机")],
        "expect": {"tried": [], "has_case": True, "scope": "other"},
    },
    {
        "id": "C12",
        "title": "其它故障不创建回充案例",
        "turns": [("user", "扫不干净怎么办")],
        "expect": {"has_case": False, "scope": "other"},
    },
    {
        "id": "C13",
        "title": "跨主题提问不串入回充已试事实",
        "turns": [("user", "回充失败"), ("assistant", "先清洁充电触点。"), ("user", "平时怎么保养")],
        "expect": {"tried": [], "scope": "other"},
    },
    {
        "id": "C14",
        "title": "结案后新故障开新案例并保留历史",
        "turns": [("user", "充不上电"), ("user", "充上了"), ("user", "又充不上电了")],
        "expect": {"case_count": 2, "status": "active", "next_action": "clear_dock_obstacles"},
    },
    {
        "id": "C15",
        "title": "“刚才试过什么”只从有证据状态回答",
        "turns": [("user", "充不上电"), ("user", "刚才试过什么")],
        "expect": {"direct_contains": "还没有你确认执行过的步骤", "tried": []},
    },
    {
        "id": "C16",
        "title": "一般选购问题不污染回充案例",
        "turns": [("user", "充不上电"), ("user", "小户型适合哪些扫地机器人")],
        "expect": {"tried": [], "scope": "other", "has_case": True},
    },
]


