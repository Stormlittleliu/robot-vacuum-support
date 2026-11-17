# -*- coding: utf-8 -*-
"""Authoring helper for the three-mainlines-v1 acceptance set.

Only builds the frozen cases/suite JSON from explicit source lines.  It does
not call the product, model, retriever, reducer or interpreter.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "development-next" / "three-mainlines-v1"
KNOW = ROOT / "assets" / "knowledge"
DOCS = [
    "assets/knowledge/故障排除.txt",
    "assets/knowledge/扫地机器人100问.pdf",
    "assets/knowledge/扫地机器人100问2.txt",
    "assets/knowledge/扫拖一体机器人100问.txt",
    "assets/knowledge/维护保养.txt",
    "assets/knowledge/选购指南.txt",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def line_quote(rel: str, line: int) -> tuple[str, dict]:
    path = ROOT / rel
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    quote = lines[line - 1].strip()
    if not quote:
        raise RuntimeError(f"empty source line {rel}:{line}")
    return quote, {
        "file": rel,
        "sha256": sha256_file(path),
        "locator": f"line {line}",
        "quote": quote,
    }


def pdf_quote(quote: str, locator: str) -> dict:
    path = ROOT / "assets/knowledge/扫地机器人100问.pdf"
    text = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    compact = "".join(quote.split())
    if compact not in "".join(text.split()):
        raise RuntimeError("PDF quote not found in pdftotext view")
    return {
        "file": "assets/knowledge/扫地机器人100问.pdf",
        "sha256": sha256_file(path),
        "locator": locator,
        "quote": quote,
        "extraction": "pdftotext -enc UTF-8 -layout; pypdf view encoding-degraded in this image",
    }


def ev(rel: str, line: int) -> dict:
    return line_quote(rel, line)[1]


def case(
    cid: str,
    *,
    group: str,
    topic: str,
    theme: str,
    primary: str,
    turns: list[dict],
    bounded: str,
    next_actions: list[str],
    must_not_claim: list[str],
    evidence: list[dict],
    required_behaviors: list[str],
    forbidden_behaviors: list[str],
    state_applicable: bool,
) -> dict:
    return {
        "id": cid,
        "family_id": cid.rsplit("-", 2)[0],
        "group": group,
        "topic": topic,
        "theme": theme,
        "primary_document": primary,
        "source": {
            "kind": "authored_for_three_mainlines_v1",
            "generation": "human-authored prompt/expected with line-level knowledge evidence",
            "no_reuse_of_holdout": True,
        },
        "origin": {
            "kind": "authored_development_case",
            "case_provenance": "authoring+evidence",
        },
        "generation": {
            "method": "fixed_12_sessions_6_mainline_6_breadth",
            "created_by": "agent",
        },
        "scoring_method": {
            "program": "structural/evidence binding; recharge state subset only",
            "semantic": "main-model advisory inspection only; no authoritative judge",
        },
        "turns": turns,
        "expected": {
            "bounded_answer": bounded,
            "next_actions": next_actions,
            "must_not_claim": must_not_claim,
            "required_behaviors": required_behaviors,
            "forbidden_behaviors": forbidden_behaviors,
            "state_applicable": state_applicable,
        },
        "evidence": evidence,
        "review": {
            "status": "not_human_reviewed",
            "note": "development advisory; semantic rubric is not an authoritative judge pass",
        },
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    recharge_normal = [
        {
            "role": "user",
            "content": "机器人回充时总在充电座前打转，最后停在一旁，充不上电。充电座周围我已经清空了，回充传感器也擦过了。",
            "expect": {
                "state": {
                    "case_status": "active",
                    "resolved": False,
                    "tried_actions_contains": [{"action_id": "clear_dock_obstacles", "outcome": "unknown"}],
                }
            },
        },
        {
            "role": "user",
            "content": "我把充电座重新摆正、确认电源灯亮着，再让它自己回充，它已经自己找到充电座并开始充电了。",
            "expect": {
                "state": {
                    "case_status": "resolved",
                    "resolved": True,
                    "closure_level": "explicit_success",
                    "observation_contains": [{"capability": "automatic_return", "result": "works"}],
                }
            },
        },
    ]
    recharge_correction = [
        {
            "role": "user",
            "content": "我已经把它放回充电座了，但还是充不上电。",
            "expect": {
                "state": {
                    "case_status": "active",
                    "resolved": False,
                    "tried_actions_contains": [{"action_id": "reseat_dock", "outcome": "failed"}],
                    "observation_contains": [{"capability": "manual_charging", "result": "fails"}],
                }
            },
        },
        {
            "role": "user",
            "content": "更正一下，刚才说错了：其实我没有把它放回充电座，只是按了回充键，自动回充也还没再试。",
            "expect": {
                "state": {
                    "case_status": "active",
                    "resolved": False,
                    "tried_actions_absent": ["reseat_dock"],
                    "not_tried_actions_contains": ["reseat_dock", "check_auto_return"],
                }
            },
        },
    ]
    suction_normal = [
        {
            "role": "user",
            "content": "最近一周扫完地，地面还是有细灰和毛发，感觉吸力比以前弱。尘盒我已经倒过，滤网也拿出来拍过灰。接下来按什么顺序排查？",
        },
        {
            "role": "user",
            "content": "按你说的检查了主刷，确实缠了不少头发，剪掉后吸口也通了，但第一次试扫还是觉得边角不干净。还需要查什么？先别让我拆电机。",
        },
    ]
    suction_correction = [
        {
            "role": "user",
            "content": "我家机器人吸力突然变得很小，是不是电机坏了？我朋友说他的机器也这样，换了电机才好。",
        },
        {
            "role": "user",
            "content": "更正一下，刚才说朋友换电机是我记混了，他其实是清理了滤网；我这台也只倒过尘盒，滤网和风道都还没检查。先别下结论。",
        },
    ]
    mopping_normal = [
        {
            "role": "user",
            "content": "拖地时不出水，水箱里明明有水，APP 出水量也调到中档了。拖布是湿的但地面几乎是干的，先检查哪里？",
        },
        {
            "role": "user",
            "content": "我按说的用牙签疏通了出水管，也重新装了水箱，第一次试还是不出水；水箱盖也拧紧了。下一步怎么办？别让我直接拆机身。",
        },
    ]
    mopping_correction = [
        {
            "role": "user",
            "content": "拖地后地板上一摊水，是不是水箱漏了？我朋友说密封圈老化就会这样，他直接换了水箱。",
        },
        {
            "role": "user",
            "content": "更正一下：我刚才说一摊水夸张了，其实是拖完有一片水痕，没有看到漏水；我也没检查过密封圈。朋友换水箱的事跟我这台无关。该怎么判断是出水量还是漏水？",
        },
    ]

    cases = [
        case(
            "ml-recharge-normal-closure",
            group="mainline",
            topic="recharge",
            theme="normal_progression",
            primary="assets/knowledge/故障排除.txt",
            turns=recharge_normal,
            bounded="第1轮承认自动回充失败且已清障，保持未解决；第2轮接受用户明确报告自动回充已开始充电，结案但原因仍未确认。",
            next_actions=["确认充电座电源和位置", "观察一次自动回充结果"],
            must_not_claim=["不得在第1轮宣称已解决", "不得把清障当成原因已被证明", "不得把建议当成用户已执行"],
            evidence=[ev("assets/knowledge/故障排除.txt", 21), ev("assets/knowledge/故障排除.txt", 183)],
            required_behaviors=["引用回充/充电座资料", "区分自动回充与手动放回", "在明确成功后才结案"],
            forbidden_behaviors=["编造用户未做的步骤", "把恢复说成零件故障已确认", "要求用户拆机"],
            state_applicable=True,
        ),
        case(
            "ml-recharge-correction-retract",
            group="mainline",
            topic="recharge",
            theme="information_correction",
            primary="assets/knowledge/故障排除.txt",
            turns=recharge_correction,
            bounded="第1轮承认手动放回已试且失败，保持未解决；第2轮接受更正、撤回已试事实，明确两项都还没验证，并给一个最低成本下一步。",
            next_actions=["重新确认自动回充是否被关闭或充电座位置是否改动", "只做一次能区分的观察"],
            must_not_claim=["不得把更正前的失败继续当成用户已试事实", "不得宣称已解决或原因已确认", "不得把助手建议当成用户已执行"],
            evidence=[ev("assets/knowledge/故障排除.txt", 183), ev("assets/knowledge/故障排除.txt", 23)],
            required_behaviors=["接受更正并撤回已试事实", "区分已试与未试", "给出可区分的下一步"],
            forbidden_behaviors=["继续引用被撤回的事实", "声称已解决", "虚构用户已执行"],
            state_applicable=True,
        ),
        case(
            "ml-suction-normal-progression",
            group="mainline",
            topic="suction",
            theme="normal_progression",
            primary="assets/knowledge/维护保养.txt",
            turns=suction_normal,
            bounded="先按低风险顺序排查尘盒、滤网、风道、主刷和吸口；第二轮承认主刷/吸口已处理但边角仍不干净，继续查边刷、风道和传感器，不建议拆电机。",
            next_actions=["检查边刷磨损/安装", "检查风道和灰尘传感器", "必要时联系售后检测电机"],
            must_not_claim=["不得直接断定电机损坏", "不得把清理主刷说成问题已解决", "不得建议用户自行拆电机"],
            evidence=[ev("assets/knowledge/故障排除.txt", 15), ev("assets/knowledge/维护保养.txt", 63), ev("assets/knowledge/维护保养.txt", 59)],
            required_behaviors=["按从低风险到高风险的顺序排查", "引用吸力下降资料", "承认部分步骤已做但效果未恢复"],
            forbidden_behaviors=["直接判电机故障", "让用户拆电机", "虚构已解决"],
            state_applicable=False,
        ),
        case(
            "ml-suction-correction-third-party",
            group="mainline",
            topic="suction",
            theme="information_correction_third_party",
            primary="assets/knowledge/维护保养.txt",
            turns=suction_correction,
            bounded="拒绝把朋友换电机的经历当成用户机器的证据；第二轮接受更正，撤回第三方事实，按尘盒/滤网/风道/主刷顺序排查，不先下电机故障结论。",
            next_actions=["检查滤网和风道", "检查主刷/吸口", "仍无效再联系售后"],
            must_not_claim=["不得把朋友换电机当成诊断依据", "不得宣称电机已坏", "不得把第三方经历写成用户已执行"],
            evidence=[ev("assets/knowledge/故障排除.txt", 15), ev("assets/knowledge/维护保养.txt", 63), ev("assets/knowledge/扫地机器人100问2.txt", 26)],
            required_behaviors=["区分第三方经历与当前机器事实", "接受更正", "给低风险排查顺序"],
            forbidden_behaviors=["依据朋友经历判故障", "建议直接换电机", "虚构用户已检查"],
            state_applicable=False,
        ),
        case(
            "ml-mopping-normal-progression",
            group="mainline",
            topic="mopping",
            theme="normal_progression",
            primary="assets/knowledge/故障排除.txt",
            turns=mopping_normal,
            bounded="先查水箱水量、出水量档位、出水管堵塞和安装密封；第二轮承认疏通/重装后仍不出水，转向密封圈、出水口和售后检测，不建议直接拆机身。",
            next_actions=["检查水箱密封圈和出水口", "确认安装到位", "仍不出水联系售后"],
            must_not_claim=["不得直接断定水泵损坏", "不得建议用户拆机身", "不得把疏通说成已修复"],
            evidence=[ev("assets/knowledge/故障排除.txt", 33), ev("assets/knowledge/扫拖一体机器人100问.txt", 26), ev("assets/knowledge/维护保养.txt", 38)],
            required_behaviors=["先做低风险水路排查", "引用拖地不出水资料", "承认已试步骤仍未恢复"],
            forbidden_behaviors=["直接拆机", "断定水泵故障", "虚构已修复"],
            state_applicable=False,
        ),
        case(
            "ml-mopping-correction-watermark",
            group="mainline",
            topic="mopping",
            theme="information_correction",
            primary="assets/knowledge/故障排除.txt",
            turns=mopping_correction,
            bounded="不把朋友换水箱当成用户漏水证据；第二轮接受更正，撤回漏水判断，区分水痕与漏水，先查出水量、拖布和密封圈，不下漏水结论。",
            next_actions=["调小出水量观察水痕", "检查拖布是否过湿/脏污", "检查水箱盖和密封圈有无渗漏"],
            must_not_claim=["不得把朋友换水箱当成诊断依据", "不得宣称水箱已漏", "不得把水痕直接等同于漏水"],
            evidence=[ev("assets/knowledge/故障排除.txt", 31), ev("assets/knowledge/故障排除.txt", 61), ev("assets/knowledge/维护保养.txt", 38)],
            required_behaviors=["区分水痕与漏水", "接受更正", "给低风险观察步骤"],
            forbidden_behaviors=["依据第三方经历判断漏水", "建议直接换水箱", "虚构用户已检查"],
            state_applicable=False,
        ),
        case(
            "br-selection-bedroom-noise",
            group="breadth",
            topic="selection",
            theme="selection_noise",
            primary="assets/knowledge/选购指南.txt",
            turns=[{"role": "user", "content": "卧室用，扫地机器人的工作噪音选多少比较合适？"}],
            bounded="给出工作噪音≤55dB的卧室建议，说明地毯模式噪音会升高，优先降噪电机机型。",
            next_actions=["按卧室场景选择低噪音档位", "查看地毯模式噪音参数"],
            must_not_claim=["不得编造具体型号价格", "不得声称所有机型都≤55dB"],
            evidence=[ev("assets/knowledge/选购指南.txt", 37)],
            required_behaviors=["引用选购指南噪音条目", "给出可执行的选购阈值"],
            forbidden_behaviors=["编造型号", "承诺绝对静音"],
            state_applicable=False,
        ),
        case(
            "br-pdf-3d-structured-light",
            group="breadth",
            topic="principle",
            theme="principle_obstacle_avoidance",
            primary="assets/knowledge/扫地机器人100问.pdf",
            turns=[{"role": "user", "content": "3D结构光避障是怎么测距和避障的？"}],
            bounded="说明通过红外点阵投影测量物体距离和形状，实现精准避障。",
            next_actions=["结合机型说明确认是否支持3D结构光"],
            must_not_claim=["不得把3D结构光与LDS激光导航混为一谈", "不得编造未给出的精度数字"],
            evidence=[pdf_quote("通过红外点阵投影测量物体距离和形状，实现精准避障。", "pdftotext -layout line 174, PDF page 4, question 50")],
            required_behaviors=["引用PDF原理条目", "解释点阵投影测距"],
            forbidden_behaviors=["把它说成激光雷达", "编造精度"],
            state_applicable=False,
        ),
        case(
            "br-100q2-frequent-pause",
            group="breadth",
            topic="usage",
            theme="usage_frequent_pause",
            primary="assets/knowledge/扫地机器人100问2.txt",
            turns=[{"role": "user", "content": "机器人清扫时老是频繁暂停，可能是什么原因，先检查什么？"}],
            bounded="先查尘盒/滤网是否堵塞、电量是否不足、机身传感器是否被灰尘遮挡，清洁后重试。",
            next_actions=["清理尘盒滤网", "确认电量", "擦拭机身传感器后重试"],
            must_not_claim=["不得直接断定主板故障", "不得建议拆机"],
            evidence=[ev("assets/knowledge/扫地机器人100问2.txt", 14)],
            required_behaviors=["给出低风险检查顺序", "引用100问2资料"],
            forbidden_behaviors=["直接判主板故障", "建议拆机"],
            state_applicable=False,
        ),
        case(
            "br-wetdry-scan-only",
            group="breadth",
            topic="usage",
            theme="usage_scan_only",
            primary="assets/knowledge/扫拖一体机器人100问.txt",
            turns=[{"role": "user", "content": "扫拖一体机器人可以只扫地不拖地吗？具体怎么设置？"}],
            bounded="说明可以在APP清扫模式关闭拖地功能，仅保留吸尘模式单独扫地。",
            next_actions=["在APP进入清扫模式", "关闭拖地功能"],
            must_not_claim=["不得声称必须拆下水箱才能只扫地", "不得编造不存在的菜单"],
            evidence=[ev("assets/knowledge/扫拖一体机器人100问.txt", 4)],
            required_behaviors=["引用扫拖一体资料", "给出APP设置路径"],
            forbidden_behaviors=["要求拆机", "编造菜单"],
            state_applicable=False,
        ),
        case(
            "br-maintenance-empty-run",
            group="breadth",
            topic="maintenance",
            theme="maintenance_empty_run",
            primary="assets/knowledge/维护保养.txt",
            turns=[{"role": "user", "content": "想定期检查电机和轮组有没有隐患，空转测试应该怎么做？"}],
            bounded="说明每月无负载开机运行1分钟，检查有无异响、卡顿。",
            next_actions=["每月空转1分钟", "记录异响/卡顿并联系售后"],
            must_not_claim=["不得声称空转可修复故障", "不得建议长时间空转"],
            evidence=[ev("assets/knowledge/维护保养.txt", 45)],
            required_behaviors=["引用维护保养条目", "给出频率和观察项"],
            forbidden_behaviors=["把空转当维修", "建议拆机"],
            state_applicable=False,
        ),
        case(
            "br-troubleshooting-side-brush",
            group="breadth",
            topic="troubleshooting",
            theme="troubleshooting_side_brush",
            primary="assets/knowledge/故障排除.txt",
            turns=[{"role": "user", "content": "边刷突然不转了，先检查什么？"}],
            bounded="先查边刷是否安装到位、转轴是否缠绕毛发、边刷电机是否异常；重新安装、清理杂物，电机损坏则联系售后。",
            next_actions=["重新安装边刷", "清理转轴杂物", "仍不转联系售后"],
            must_not_claim=["不得直接断定电机烧毁", "不得建议用户自行更换电机"],
            evidence=[ev("assets/knowledge/故障排除.txt", 19)],
            required_behaviors=["按安装/缠绕/电机顺序排查", "引用故障排除资料"],
            forbidden_behaviors=["直接判电机烧毁", "建议自行换电机"],
            state_applicable=False,
        ),
    ]

    docs = []
    for rel in DOCS:
        path = ROOT / rel
        docs.append(
            {
                "path": rel,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "kind": "knowledge",
                "extraction": "pdftotext -enc UTF-8 -layout" if path.suffix == ".pdf" else "utf-8 text",
            }
        )
    suite = {
        "schema_version": "three-mainlines-suite/v1",
        "suite_id": "three-mainlines-v1",
        "revision": "v1",
        "created_at": "2026-11-17",
        "cases_file": "cases.json",
        "purpose": "固定 12 独立 session：3 个真实主线各 2 case（回充、吸力/清扫效果、拖地出水），6 条广度各覆盖一份现有知识资料；比例按 session 计，不按 turn。",
        "counts": {
            "total": 12,
            "mainline": 6,
            "breadth": 6,
            "user_turns": 18,
            "mainline_topics": {"recharge": 2, "suction": 2, "mopping": 2},
        },
        "proportion": {
            "mainline": 0.5,
            "breadth": 0.5,
            "rule": "6/12 sessions 为主线，6/12 sessions 为广度；主线每 case 2 turn，广度每 case 1 turn，共 18 用户 turn。",
        },
        "required_topics": ["recharge", "suction", "mopping"],
        "required_breadth_documents": DOCS,
        "documents": docs,
        "target": {
            "image": "rvs-backend:smoke",
            "image_id": "sha256:cd562e9c43461640e1257e73e7b621bb0a6e5801707c063c66e13398328eadf3",
            "compose_project": "rvs-eval-three",
            "public_port": 18081,
            "data_volume": "rvs-eval-three_rvs-data",
            "artifact_id_at_freeze": "robot_vacuum-v4-7d264b320c",
            "entry": "HTTP/SSE through nginx at http://127.0.0.1:18081",
            "existing_service_untouched": "http://127.0.0.1:18080 (rvssmoke)",
        },
        "execution": {
            "mode": "live_once",
            "host": "127.0.0.1",
            "concurrency": 2,
            "max_sessions": 12,
            "max_turns_per_session": 2,
            "turn_timeout_seconds": 180,
            "independent_session_per_case": True,
            "no_green_retry": True,
            "transport_retry_policy": "at most one retry for a transport error only; every attempt recorded",
        },
        "scoring": {
            "program": "structural/evidence binding only: answer nonempty, retrieval attempted, citation filename, expected source presence, recharge state subset",
            "semantic": "main-model advisory inspection only; no authoritative judge",
            "authoritative_pass_claimed": False,
            "not_success_rate": True,
            "not_holdout": True,
        },
        "state_policy": "Only recharge cases carry dedicated business state expectations; suction/mopping are transcript-only and must not require fabricated state.",
        "human_pressure_test": {"status": "not_run", "note": "optional, not blocking this development run"},
    }
    cases_doc = {
        "schema_version": "three-mainlines-cases/v1",
        "suite_id": "three-mainlines-v1",
        "revision": "v1",
        "cases": cases,
    }
    (OUT / "suite.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "cases.json").write_text(json.dumps(cases_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    freeze = {
        "schema_version": "three-mainlines-freeze/v1",
        "frozen_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "suite_sha256": sha256_file(OUT / "suite.json"),
        "cases_sha256": sha256_file(OUT / "cases.json"),
        "case_hashes": {
            c["id"]: hashlib.sha256(json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            for c in cases
        },
        "policy": "expected/evidence frozen before model calls; never edited after run",
    }
    (OUT / "freeze.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(cases), "turns": sum(len(c["turns"]) for c in cases), "suite_sha256": freeze["suite_sha256"], "cases_sha256": freeze["cases_sha256"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
