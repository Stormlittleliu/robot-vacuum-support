# -*- coding: utf-8 -*-
"""Development families (16 families x 2 variants = 32 cases)."""
from __future__ import annotations

from seed_helpers import F, T, V

KB = "assets/knowledge/"

FAMILIES = [
    F(
        "fam-dev-direct-recharge-triage",
        "direct_qa",
        "development",
        "回充失败的多分支低风险排查",
        "给出多分支低风险检查；不把清障当唯一首步或保证；不直接判硬件。",
        ["ev-fault-recharge-notfound", "ev-1002-recharge-space", "ev-1002-auto-return", "ev-maint-ir", "ev-pdf-no-return", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-direct-recharge-triage-01",
                "回充失败，充电座旁有拖鞋",
                [
                    T(
                        "user",
                        "我家机器人回充总是失败，充电座在客厅，周围有拖鞋，应该怎么排查？",
                        claims=[
                            "可先清理充电座周围障碍物，并留出充电座前方约3米、左右约1米的无遮挡空间。",
                            "还应检查回充传感器和充电座红外感应区是否积灰，以及充电座是否摆放平整、位置是否被移动。",
                            "可检查APP是否关闭自动回充、地图中充电座位置是否被改动。",
                            "清理障碍物只是低风险排查之一，不是保证解决，也不是所有回充问题的唯一首步。",
                        ],
                        forbidden=[
                            "不要仅凭“回充失败”断定主板或回充传感器硬件损坏。",
                            "不要编造资料中没有的故障代码、维修价格或官方保修承诺。",
                            "不要建议自行拆机或打磨内部触点。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "recharge",
                            "tried_actions": [],
                            "not_tried_actions": ["clear_dock_obstacles", "clean_sensors", "check_power", "check_auto_return"],
                        },
                        behaviors=[
                            "给出可执行的低风险排查项，并询问用户已经试过哪些步骤。",
                            "事实性步骤附真实来源文件名，不把通用资料说成官方授权售后。",
                        ],
                        sources=[KB + "故障排除.txt", KB + "扫地机器人100问2.txt", KB + "维护保养.txt", KB + "扫地机器人100问.pdf"],
                    )
                ],
                seed_ids=["rs-01-direct-recharge"],
            ),
            V(
                "rsv1-dev-direct-recharge-triage-02",
                "先清障是不是就够了",
                [
                    T(
                        "user",
                        "回充失败，是不是先把充电座周围清干净就行了？",
                        claims=[
                            "清理充电座周围是合理的低风险步骤，但不能保证解决，也不能作为唯一排查项。",
                            "还应检查充电座供电和指示灯、回充传感器与红外感应区、APP自动回充设置及地图位置。",
                            "如果清洁和供电都正常仍失败，再考虑进一步检测，不直接下硬件结论。",
                        ],
                        forbidden=[
                            "不要说“只要清障就一定能恢复”。",
                            "不要直接建议更换主板或回充传感器。",
                            "不要把清障写成所有回充问题的固定首步。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "recharge",
                            "tried_actions": [],
                            "not_tried_actions": ["clear_dock_obstacles", "check_power", "clean_sensors", "check_auto_return"],
                        },
                        behaviors=[
                            "先回答用户的条件式问题，再追问已试步骤。",
                            "不把一个低风险步骤写成保证或唯一答案。",
                        ],
                        sources=[KB + "故障排除.txt", KB + "扫地机器人100问2.txt", KB + "维护保养.txt"],
                    )
                ],
                seed_ids=["rs-01-direct-recharge"],
            ),
        ],
        human_focus=["是否仍把清障写成唯一首步", "是否把多原因排查误写成硬件结论"],
    ),
    F(
        "fam-dev-direct-mop-no-water",
        "direct_qa",
        "development",
        "拖地不出水的水路低风险排查",
        "先查水箱、出水量、出水口/出水管/拖布湿润；不直接判水泵，不指导拆泵。",
        ["ev-fault-mop-no-water", "ev-fault-mop-dry", "ev-maint-mop-outlet", "ev-sweepmop-outlet", "ev-sweepmop-water-volume", "ev-maint-no-diy", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-direct-mop-no-water-01",
                "水箱有水但拖地不出水",
                [
                    T(
                        "user",
                        "拖地不出水，水箱里还有水，是不是水泵坏了？",
                        claims=[
                            "先检查水箱是否有水、APP出水量是否被调到最低或关闭，以及出水管和出水口是否堵塞。",
                            "可加注清水、调高出水量，并用牙签轻轻疏通出水管；不要直接判定水泵损坏。",
                            "若清理后仍不出水，再按机型说明检测或联系官方售后，不指导自行拆泵。",
                        ],
                        forbidden=[
                            "不要仅凭不出水断言水泵故障。",
                            "不要建议拆解水泵或内部管路。",
                            "不要把回充传感器当作拖地不出水的原因。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "mop",
                            "tried_actions": [],
                            "not_tried_actions": ["check_water_tank", "adjust_water_volume", "clear_water_outlet"],
                        },
                        behaviors=[
                            "先给低风险检查顺序，再问是否清理过出水口。",
                            "引用真实来源文件名，不把通用资料说成官方授权售后。",
                        ],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫拖一体机器人100问.txt"],
                    )
                ],
                seed_ids=["rs-06-switch-topic-mop-water"],
            ),
            V(
                "rsv1-dev-direct-mop-no-water-02",
                "木地板出水量已调高仍不出水",
                [
                    T(
                        "user",
                        "拖地时不出水，木地板，出水量已经调到高档，还是干。",
                        claims=[
                            "出水量高档不等于水路实际出水；检查水箱水量、出水口和出水管是否堵塞。",
                            "检查拖布是否湿润或安装到位，避免把“设置已调高”当成已恢复。",
                            "清理后仍不出水再考虑检测，不直接换水泵；木地板上不要为了出水而调到积水。",
                        ],
                        forbidden=[
                            "不要因为用户已调高出水量就跳过水路检查。",
                            "不要建议在木地板上加大水量到积水。",
                            "不要拆泵或断言电机损坏。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "mop",
                            "tried_actions": [{"action_id": "adjust_water_volume", "outcome": "unknown"}],
                            "not_tried_actions": ["check_water_tank", "clear_water_outlet"],
                        },
                        behaviors=[
                            "区分“设置已调高”和“水路实际出水”。",
                            "给一条可逆的低风险检查，并说明木地板的水量边界。",
                        ],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫拖一体机器人100问.txt"],
                    )
                ],
                seed_ids=["rs-06-switch-topic-mop-water"],
            ),
        ],
        human_focus=["是否把不出水直接归因水泵", "是否忽略木地板水量边界"],
    ),
    F(
        "fam-dev-direct-suction-decline",
        "direct_qa",
        "development",
        "吸力下降的堵塞/耗材优先排查",
        "先查尘盒、滤网、风道、主刷/吸口；滤网需干燥未破损；不先判电机。",
        ["ev-fault-suction", "ev-maint-suction", "ev-1002-suction", "ev-pdf-filter", "ev-maint-filter", "ev-fault-mainbrush"],
        [
            V(
                "rsv1-dev-direct-suction-decline-01",
                "尘盒刚倒过但吸力仍小",
                [
                    T(
                        "user",
                        "吸力越来越小，尘盒刚倒过，还是吸不干净，怎么办？",
                        claims=[
                            "依次检查滤网、风道、主刷和吸口是否堵塞或缠绕，而不只检查尘盒。",
                            "清洗滤网后要阴凉晾干，清理主刷和吸口；滤网破损或老化时更换。",
                            "不要仅凭吸力下降判断电机损坏，也不要在滤网潮湿时直接装机。",
                        ],
                        forbidden=[
                            "不要只凭吸力下降判断电机损坏。",
                            "不要建议用非原装滤网或自行拆电机。",
                            "不要忽略滤网未干、风道或吸口堵塞。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "suction",
                            "tried_actions": [{"action_id": "empty_dust_box", "outcome": "success"}],
                            "not_tried_actions": ["clean_filter", "check_air_duct", "clean_main_brush"],
                        },
                        behaviors=[
                            "给出耗材和堵塞优先的排查顺序。",
                            "追问滤网多久没换、是否水洗后未干。",
                        ],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫地机器人100问2.txt", KB + "扫地机器人100问.pdf"],
                    )
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-dev-direct-suction-decline-02",
                "滤网洗过晾了一晚仍吸力小",
                [
                    T(
                        "user",
                        "吸力下降，滤网用水洗过，晾了一晚，还是不行。",
                        claims=[
                            "确认滤网是否完全干燥、是否破损或老化；湿滤网或破损滤网都会影响吸力。",
                            "继续检查风道、吸口和主刷是否堵塞或缠绕，不只停在滤网。",
                            "如果滤网已老化或破损，可更换；仍不足时再考虑进一步检测，不直接判电机。",
                        ],
                        forbidden=[
                            "不要说水洗后一定恢复。",
                            "不要建议湿滤网直接装机。",
                            "不要仅凭一次水洗就断言电机故障。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "suction",
                            "tried_actions": [{"action_id": "clean_filter", "outcome": "failed"}],
                            "not_tried_actions": ["check_air_duct", "clean_main_brush", "replace_filter"],
                        },
                        behaviors=[
                            "保留“已洗滤网”事实，转向风道、主刷和吸口。",
                            "追问滤网使用时长和是否完全干燥。",
                        ],
                        sources=[KB + "维护保养.txt", KB + "故障排除.txt", KB + "扫地机器人100问2.txt", KB + "扫地机器人100问.pdf"],
                    )
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["是否从堵塞/耗材直接跳到电机", "是否检查滤网干燥与破损"],
    ),
    F(
        "fam-dev-direct-base-power",
        "direct_qa",
        "development",
        "充电座指示灯不亮的供电链路排查",
        "先查电源线、插座、适配器和充电座；不拆充电座；损坏时原装更换/官方售后。",
        ["ev-fault-base-power", "ev-maint-power-cord", "ev-maint-original-cord", "ev-guide-base-clean", "ev-maint-no-diy"],
        [
            V(
                "rsv1-dev-direct-base-power-01",
                "充电座灯不亮、放上去没反应",
                [
                    T(
                        "user",
                        "充电座指示灯不亮，机器人放上去没反应，怎么办？",
                        claims=[
                            "检查电源线是否插紧、插座是否通电、适配器和线缆是否破损；可换一个已知有电的插座测试。",
                            "如果充电座损坏，按说明更换或联系官方售后，不自行拆修。",
                            "区分充电座不供电和机器人电池故障，不直接下主板结论。",
                        ],
                        forbidden=[
                            "不要教用户拆充电座、短接或焊接。",
                            "不要建议使用非原装电源线。",
                            "不要直接说主板坏了。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "recharge",
                            "tried_actions": [],
                            "not_tried_actions": ["check_power", "check_adapter"],
                        },
                        behaviors=[
                            "先做供电链路排查，再问是否换过插座或线缆。",
                            "把充电座不供电与机器人电池故障分开。",
                        ],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "选购指南.txt"],
                    )
                ],
                seed_ids=["rs-02-after-cleared-and-wiped"],
            ),
            V(
                "rsv1-dev-direct-base-power-02",
                "换了插座充电座仍不亮，能自己修吗",
                [
                    T(
                        "user",
                        "充电座灯不亮，我换了插座还是不行，能自己修充电座吗？",
                        claims=[
                            "已换插座仍不亮，继续检查电源线和适配器是否松动、破损，确认充电座是否损坏。",
                            "不建议自行拆开充电座；损坏时更换原装充电座或联系官方售后。",
                            "确认“已换插座”已经试过，不再重复同一建议。",
                        ],
                        forbidden=[
                            "不要提供拆充电座、焊接或短接步骤。",
                            "不要建议非原装适配器。",
                            "不要断言是电池损坏。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "recharge",
                            "tried_actions": [{"action_id": "check_power", "outcome": "failed"}],
                            "not_tried_actions": ["check_adapter", "replace_dock"],
                        },
                        behaviors=[
                            "确认已换插座，不重复该步骤。",
                            "给出安全边界，并建议核对型号后使用原装替换。",
                        ],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    )
                ],
                seed_ids=["rs-02-after-cleared-and-wiped"],
            ),
        ],
        human_focus=["是否提供拆充电座步骤", "是否重复“换插座”"],
    ),
    F(
        "fam-dev-multi-recharge-tried-advance",
        "multi_turn",
        "development",
        "已清障并擦触点后仍失败：推进而不是重复",
        "保留已试清障/擦触点/传感器，不重复为首要建议；转向供电、红外、摆放或手动放回。",
        ["ev-fault-recharge-notfound", "ev-fault-manual-return", "ev-fault-base-power", "ev-maint-contacts", "ev-maint-ir", "ev-maint-no-diy"],
        [
            V(
                "rsv1-dev-multi-recharge-tried-advance-01",
                "清障、擦触点和传感器都做了仍失败",
                [
                    T(
                        "user",
                        "回充失败，找不到充电座。",
                        claims=["先建立“回充失败、找不到充电座”的现象，给出低风险检查或先问已试步骤。"],
                        forbidden=["不要在用户未说明前假设清障、擦触点已经完成。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["clear_dock_obstacles", "clean_sensors", "check_power"]},
                        behaviors=["先确认现象，不急于下硬件结论。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                    T("assistant_fixture", "可以先清理充电座周围3米，再用干布擦拭回充传感器和充电触点，然后告诉我结果。"),
                    T(
                        "user",
                        "都做了，还是回不去。",
                        claims=[
                            "确认用户已经清障、擦拭充电触点和回充传感器，不再把这三步作为首要的未试建议。",
                            "下一步可检查充电座指示灯和供电、充电座红外信号、充电座摆放，或把机器人手动放回充电座测试。",
                            "如果手动放回能充电、自动回充仍失败，可提示检测回充传感器或充电座信号，并建议联系官方售后。",
                        ],
                        forbidden=[
                            "不要再次要求用户清理充电座周围或擦拭触点作为唯一建议。",
                            "不要断定主板坏了。",
                            "不要建议用户自行拆机维修。",
                        ],
                        state={
                            "case_status": "active",
                            "scope": "recharge",
                            "tried_actions": [
                                {"action_id": "clear_dock_obstacles", "outcome": "failed"},
                                {"action_id": "clean_contacts", "outcome": "failed"},
                                {"action_id": "clean_sensors", "outcome": "failed"},
                            ],
                            "not_tried_actions": ["check_power", "reseat_dock"],
                        },
                        behaviors=["保留已试状态，给出尚未尝试的下一步。", "必要时追问充电座指示灯是否亮、是否通电。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=["rs-02-after-cleared-and-wiped"],
            ),
            V(
                "rsv1-dev-multi-recharge-tried-advance-02",
                "触点传感器都擦了仍失败",
                [
                    T(
                        "user",
                        "回充失败，充电座周围已经清了。",
                        claims=["记录已清障，并追问是否擦过充电触点和回充传感器。"],
                        forbidden=["不要把已清障说成未做。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}], "not_tried_actions": ["clean_contacts", "clean_sensors", "check_power"]},
                        behaviors=["确认已清障后，再把触点和传感器作为下一步。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                    T("assistant_fixture", "再擦一下充电触点和回充传感器。"),
                    T(
                        "user",
                        "触点擦了，传感器也擦了，还是不行。",
                        claims=[
                            "确认已清障、已擦触点和回充传感器，不再重复这三步。",
                            "下一步检查充电座供电/指示灯、红外信号、摆放位置，或手动放回测试。",
                            "不直接判定硬件损坏，也不建议拆机。",
                        ],
                        forbidden=["不要把已擦触点和传感器说成未做。", "不要重复清障/擦触点作为唯一建议。", "不要建议拆机。"],
                        state={
                            "case_status": "active",
                            "scope": "recharge",
                            "tried_actions": [
                                {"action_id": "clear_dock_obstacles", "outcome": "success"},
                                {"action_id": "clean_contacts", "outcome": "failed"},
                                {"action_id": "clean_sensors", "outcome": "failed"},
                            ],
                            "not_tried_actions": ["check_power", "reseat_dock"],
                        },
                        behaviors=["从已试步骤推进到供电/摆放/手动放回。", "追问充电座指示灯状态。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=["rs-02-after-cleared-and-wiped"],
            ),
        ],
        human_focus=["已试步骤是否被记住", "下一步是否真正向前推进"],
    ),
    F(
        "fam-dev-multi-recharge-negation",
        "multi_turn",
        "development",
        "用户明确否定未试：不能记成已试",
        "否定动作记为未试；先给低风险步骤；不跳售后；不重复已明确回答的追问。",
        ["ev-fault-recharge-notfound", "ev-maint-contacts", "ev-fault-base-power", "ev-1002-recharge-space"],
        [
            V(
                "rsv1-dev-multi-recharge-negation-01",
                "清障和擦触点都没试过",
                [
                    T("user", "机器人回充失败，怎么办？", claims=["先给低风险排查方向，或先问用户已经试过哪些步骤。"], forbidden=["不要一上来就建议售后检测或更换主板。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": []}, behaviors=["不假设用户已经做过清障、擦触点。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                    T("assistant_fixture", "你清理过充电座周围、擦过充电触点吗？"),
                    T(
                        "user",
                        "都没试过，别让我拆机。",
                        claims=[
                            "把清障和擦触点都记为未试，不能记成已试。",
                            "先建议清理充电座周围障碍物、用干布擦拭充电触点和回充传感器这些低风险步骤。",
                            "可同时检查充电座摆放、供电和APP自动回充设置；不直接跳到售后或拆机。",
                        ],
                        forbidden=["不要把“没试过”记成“已试过”。", "不要直接跳到售后检测或主板故障。", "不要重复追问用户已经明确回答过的“没试过”。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["clear_dock_obstacles", "clean_contacts", "check_power"]},
                        behaviors=["把否定信息作为未试状态，而不是已试状态。", "下一步给最小可执行的低风险步骤。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫地机器人100问2.txt"],
                    ),
                ],
                seed_ids=["rs-03-denies-not-tried"],
            ),
            V(
                "rsv1-dev-multi-recharge-negation-02",
                "擦过触点但还没清障",
                [
                    T("user", "回充失败，我想先问一下要查什么。", claims=["给出低风险检查项或先问已试步骤。"], forbidden=["不要假设所有步骤都做过。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": []}, behaviors=["先建立现象和已试信息。"], sources=[KB + "故障排除.txt"]),
                    T("assistant_fixture", "清障和擦触点分别试过吗？"),
                    T(
                        "user",
                        "我还没清障，但触点擦过了。",
                        claims=[
                            "把擦触点记为已试，把清障记为未试，不能混在一起。",
                            "下一步建议清理充电座周围障碍物，不重复把擦触点当成未做。",
                            "仍失败时再检查供电、红外和摆放，不跳到售后。",
                        ],
                        forbidden=["不要说用户已经清障。", "不要重复要求擦触点作为未试步骤。", "不要直接建议更换主板。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clean_contacts", "outcome": "unknown"}], "not_tried_actions": ["clear_dock_obstacles", "check_power"]},
                        behaviors=["区分已试和未试动作。", "优先补做未试的低风险步骤。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=["rs-03-denies-not-tried"],
            ),
        ],
        human_focus=["否定是否被误记为已试", "未试步骤是否被重复追问"],
    ),
    F(
        "fam-dev-multi-recharge-correction",
        "multi_turn",
        "development",
        "用户更正已试事实：以最后一次明确陈述为准",
        "更正覆盖旧事实；不保留错误已试；不重复被更正掉的建议；不指责。",
        ["ev-maint-contacts", "ev-fault-recharge-notfound", "ev-fault-base-power"],
        [
            V(
                "rsv1-dev-multi-recharge-correction-01",
                "先说擦了触点，后更正没擦",
                [
                    T("user", "回充失败，我已经擦过充电触点了。", claims=["记录用户声称已擦充电触点，并继续了解回充失败现象。"], forbidden=["不要编造用户没有说过的其他已试步骤。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clean_contacts", "outcome": "unknown"}], "not_tried_actions": ["check_power"]}, behaviors=["把“已擦触点”作为待确认的已试信息。"], sources=[KB + "维护保养.txt", KB + "故障排除.txt"]),
                    T("assistant_fixture", "那再确认一下充电座供电和回充传感器。"),
                    T(
                        "user",
                        "等等，我说错了，触点没擦，只清了充电座周围的障碍物。",
                        claims=[
                            "接受更正，把已试步骤改为“已清障、未擦触点”。",
                            "建议用干布擦拭充电触点和回充传感器，而不是声称用户已经擦过。",
                            "不重复要求已完成的清障步骤作为首要建议。",
                        ],
                        forbidden=["不要保留“已擦触点”的旧状态。", "不要因用户更正而指责或反复盘问。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}], "not_tried_actions": ["clean_contacts", "check_power"]},
                        behaviors=["以最后一次明确更正为准更新状态。", "在回复中体现更正。"],
                        sources=[KB + "维护保养.txt", KB + "故障排除.txt"],
                    ),
                ],
                seed_ids=["rs-07-user-correction"],
            ),
            V(
                "rsv1-dev-multi-recharge-correction-02",
                "先说换了充电座，后更正是挪位置",
                [
                    T("user", "回充失败，我已经换了一个新的充电座。", claims=["记录用户声称已换充电座；不据此直接判主板。"], forbidden=["不要编造新充电座的品牌或价格。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "replace_dock", "outcome": "unknown"}], "not_tried_actions": ["check_power", "clean_sensors"]}, behaviors=["保留“已换充电座”作为待确认事实。"], sources=[KB + "故障排除.txt"]),
                    T("assistant_fixture", "既然换过座，检查下传感器和供电。"),
                    T(
                        "user",
                        "更正一下，没换新座，只是把旧座挪了个位置。",
                        claims=[
                            "把“换新充电座”更正为“挪动旧充电座”，不继续按已换新座回答。",
                            "下一步检查充电座摆放、供电和红外/传感器，不重复建议换充电座。",
                            "不因更正而断定主板故障。",
                        ],
                        forbidden=["不要继续声称用户已换新充电座。", "不要再次建议换充电座作为首要步骤。", "不要建议拆机。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "reseat_dock", "outcome": "unknown"}], "not_tried_actions": ["check_power", "clean_sensors"]},
                        behaviors=["以最后一次明确更正为准。", "把建议从“换座”转向摆放/供电/传感器。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=["rs-07-user-correction"],
            ),
        ],
        human_focus=["旧事实是否被覆盖", "是否重复被更正掉的建议"],
    ),
    F(
        "fam-dev-multi-recharge-closure",
        "multi_turn",
        "development",
        "成功结案与“手动能充”假结案",
        "只有明确自动回充恢复才结案；手动放回能充电是部分成功，不能结案。",
        ["ev-maint-base-level", "ev-maint-ir", "ev-fault-manual-return", "ev-maint-repeat-return", "ev-pdf-no-return"],
        [
            V(
                "rsv1-dev-multi-recharge-closure-01",
                "清障后已能自动回充",
                [
                    T("user", "回充失败，充电座周围有杂物。", claims=["建议清理充电座周围障碍物，并检查充电座摆放和回充传感器。"], forbidden=["不要在用户尚未尝试前就建议拆机或更换主板。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["clear_dock_obstacles"]}, behaviors=["给出低风险、可执行的排查步骤。"], sources=[KB + "维护保养.txt", KB + "故障排除.txt"]),
                    T("assistant_fixture", "先清理周围，再试一次。"),
                    T(
                        "user",
                        "清干净后已经能自己回充了，谢谢。",
                        claims=["确认当前回充问题已经解决，结束本次排查。", "可给一条简短维护建议：保持充电座周围无遮挡，定期清理充电座红外感应区。"],
                        forbidden=["不要在用户已成功回充后继续要求拆机或售后检测。", "不要把已解决的回充问题继续当作待排查故障。"],
                        state={"case_status": "resolved", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}]},
                        behaviors=["识别成功结案信号，回复简洁，不再追加无关步骤。"],
                        sources=[KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=["rs-05-success-close"],
            ),
            V(
                "rsv1-dev-multi-recharge-closure-02",
                "手动放回能充但自动回充仍失败",
                [
                    T("user", "回充失败，机器人总是找不到充电座。", claims=["先建立现象并给低风险检查。"], forbidden=["不要直接判硬件。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["clear_dock_obstacles", "clean_sensors"]}, behaviors=["先确认是找不到、对不准还是充电座不供电。"], sources=[KB + "故障排除.txt"]),
                    T("assistant_fixture", "先清障、擦传感器。"),
                    T(
                        "user",
                        "手动放回能充电，但自动回充还是失败。",
                        claims=[
                            "手动放回能充电不等于自动回充已解决，不能结案。",
                            "下一步检查回充传感器、充电座红外信号和摆放位置，或做手动放回对照测试。",
                            "若自动回充仍失败，可提示进一步检测或联系官方售后。",
                        ],
                        forbidden=["不要把“手动能充”说成问题已解决。", "不要停止排查。", "不要建议拆机。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}, {"action_id": "clean_sensors", "outcome": "success"}, {"action_id": "reseat_dock", "outcome": "success"}], "not_tried_actions": ["check_power"]},
                        behaviors=["区分手动充电成功与自动回充成功。", "保持案例 active，给出下一步。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=["rs-05-success-close", "rs-16-false-premise-mainboard"],
            ),
        ],
        human_focus=["是否把部分成功当结案", "结案后是否停止过度追问"],
    ),
    F(
        "fam-dev-multi-topic-switch-mop",
        "multi_turn",
        "development",
        "回充话题中切换到拖地故障",
        "切换主题不串来源；保留旧回充未解决；不把拖地问题归因回充传感器。",
        ["ev-fault-mop-no-water", "ev-fault-water-leak", "ev-fault-recharge-notfound", "ev-maint-mop-outlet", "ev-sweepmop-outlet", "ev-1002-water-leak", "ev-maint-no-diy", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-multi-topic-switch-mop-01",
                "回充失败后转问拖地不出水",
                [
                    T("user", "回充失败，充电座周围有遮挡。", claims=["建议清理充电座周围障碍物，并保留回充问题为未完成事项。"], forbidden=["不要建议自行拆机。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["clear_dock_obstacles"]}, behaviors=["建立回充任务状态。"], sources=[KB + "故障排除.txt"]),
                    T(
                        "user",
                        "先不说回充了，拖地不出水怎么办？",
                        claims=[
                            "识别为新故障主题“拖地不出水”，切换到该主题回答。",
                            "检查水箱是否有水、出水量是否调至最低、出水管/出水口是否堵塞；可加注清水、调高出水量、用牙签轻轻疏通。",
                            "保留旧回充问题为未解决，不把回充结论套到拖地上。",
                        ],
                        forbidden=["不要把拖地不出水归因于回充传感器或充电座。", "不要继续追问回充而忽略当前主题。", "不要建议自行拆解水泵或内部管路。"],
                        state={"case_status": "active", "scope": "mop", "tried_actions": [], "not_tried_actions": ["check_water_tank", "adjust_water_volume", "clear_water_outlet"]},
                        behaviors=["当前回答聚焦新主题，来源作用域不串到回充资料。", "旧回充任务保留为未解决。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫拖一体机器人100问.txt"],
                    ),
                ],
                seed_ids=["rs-06-switch-topic-mop-water"],
            ),
            V(
                "rsv1-dev-multi-topic-switch-mop-02",
                "回充失败后转问水箱漏水",
                [
                    T("user", "回充失败，已经清了充电座周围。", claims=["记录已清障并保留回充任务。"], forbidden=["不要重复清障作为唯一建议。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}], "not_tried_actions": ["check_power"]}, behaviors=["保留已清障事实。"], sources=[KB + "故障排除.txt"]),
                    T(
                        "user",
                        "对了，拖地时水箱漏水，怎么办？",
                        claims=[
                            "切换到水箱漏水主题，检查水箱盖是否拧紧、密封圈是否老化/脱落、出水管是否堵塞。",
                            "可拧紧水箱盖、更换老化密封圈、疏通出水管；加水不要过满。",
                            "保留旧回充任务；不把漏水归因于回充传感器。",
                        ],
                        forbidden=["不要把漏水说成回充故障。", "不要建议自行拆水泵或内部管路。", "不要忽略用户已清障的回充事实。"],
                        state={"case_status": "active", "scope": "mop", "tried_actions": [], "not_tried_actions": ["check_tank_seal", "clear_water_outlet"]},
                        behaviors=["来源作用域切到水箱/密封圈。", "旧回充任务保留。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫地机器人100问2.txt"],
                    ),
                ],
                seed_ids=["rs-06-switch-topic-mop-water"],
            ),
        ],
        human_focus=["主题切换后来源是否串域", "旧任务是否被丢弃"],
    ),
    F(
        "fam-dev-negation-filter-not-cleaned",
        "negation_correction_ambiguity",
        "development",
        "吸力问题中“过滤网没清”被误当已清",
        "区分过滤网与尘盒；否定/更正为未清；不把助手建议升级成用户已做。",
        ["ev-fault-suction", "ev-maint-suction", "ev-maint-filter", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-negation-filter-not-cleaned-01",
                "先说清过滤网，后更正是没清",
                [
                    T("user", "吸力下降，我已经清理过滤网了。", claims=["记录用户声称已清过滤网，并追问是否干燥、是否同时检查风道和主刷。"], forbidden=["不要编造用户已更换滤网。"], state={"case_status": "active", "scope": "suction", "tried_actions": [{"action_id": "clean_filter", "outcome": "unknown"}], "not_tried_actions": ["check_air_duct", "clean_main_brush"]}, behaviors=["把“已清过滤网”作为待确认事实。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                    T("assistant_fixture", "滤网晾干了吗？风道和主刷也看一下。"),
                    T(
                        "user",
                        "没有，我根本没清过滤网，刚才说错了。",
                        claims=["接受更正，把过滤网改为未清，不继续按已清回答。", "建议清理过滤网并阴凉晾干，再检查风道、主刷和吸口。", "不因为没有清理过滤网而直接判电机故障。"],
                        forbidden=["不要保留“已清过滤网”的旧状态。", "不要把“没清过滤网”记成已试。", "不要因更正而指责用户。"],
                        state={"case_status": "active", "scope": "suction", "tried_actions": [], "not_tried_actions": ["clean_filter", "check_air_duct"]},
                        behaviors=["以最后一次明确更正为准。", "把过滤网放回未试步骤。"],
                        sources=[KB + "维护保养.txt", KB + "故障排除.txt"],
                    ),
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-dev-negation-filter-not-cleaned-02",
                "清的是尘盒不是过滤网",
                [
                    T("user", "吸力下降，过滤网没清过，别当成清过。", claims=["把过滤网记为未清；不假设用户已做。"], forbidden=["不要编造已清过滤网。"], state={"case_status": "active", "scope": "suction", "tried_actions": [], "not_tried_actions": ["clean_filter"]}, behaviors=["先确认否定，再给低风险步骤。"], sources=[KB + "维护保养.txt"]),
                    T("assistant_fixture", "先清滤网，再看看风道。"),
                    T(
                        "user",
                        "我清的是尘盒，不是滤网。",
                        claims=["区分尘盒与过滤网：尘盒已清，过滤网仍未清。", "建议清理过滤网并晾干，再检查风道/吸口；不把尘盒清理当成滤网已清。", "不直接判电机故障。"],
                        forbidden=["不要把尘盒已清等同于过滤网已清。", "不要重复说用户已清过滤网。"],
                        state={"case_status": "active", "scope": "suction", "tried_actions": [{"action_id": "empty_dust_box", "outcome": "success"}], "not_tried_actions": ["clean_filter", "check_air_duct"]},
                        behaviors=["按对象区分已试动作。", "下一步针对过滤网。"],
                        sources=[KB + "维护保养.txt", KB + "故障排除.txt"],
                    ),
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["是否把尘盒与滤网混为一个动作", "否定是否被保留"],
    ),
    F(
        "fam-dev-correction-mop-water-level",
        "negation_correction_ambiguity",
        "development",
        "拖地不出水的水箱前提被更正",
        "更正水箱水位/安装前提；不继续按“水箱满/已装好”回答；下一步对准真实原因。",
        ["ev-fault-mop-no-water", "ev-sweepmop-water-volume", "ev-maint-mop-outlet", "ev-fault-water-leak"],
        [
            V(
                "rsv1-dev-correction-mop-water-level-01",
                "先说水箱满，后更正只剩一点水",
                [
                    T("user", "拖地不出水，水箱是满的。", claims=["记录水箱满的前提；检查出水量和出水口/出水管。"], forbidden=["不要直接判水泵故障。"], state={"case_status": "active", "scope": "mop", "tried_actions": [{"action_id": "check_water_tank", "outcome": "success"}], "not_tried_actions": ["adjust_water_volume", "clear_water_outlet"]}, behaviors=["把水箱满作为已确认前提。"], sources=[KB + "故障排除.txt", KB + "扫拖一体机器人100问.txt"]),
                    T("assistant_fixture", "把出水量调高，疏通出水口。"),
                    T(
                        "user",
                        "更正：水箱不是满的，只剩一点水。",
                        claims=["接受更正，把水箱水位改为不足；先加注清水，再检查出水量和出水口。", "不继续按“水箱满”回答，也不把不出水直接归因水泵。", "不重复只让用户调高出水量。"],
                        forbidden=["不要坚持水箱是满的。", "不要仅调高出水量而不补水。", "不要建议拆泵。"],
                        state={"case_status": "active", "scope": "mop", "tried_actions": [], "not_tried_actions": ["fill_water_tank", "clear_water_outlet"]},
                        behaviors=["以最后一次更正为准。", "先补足水，再查水路。"],
                        sources=[KB + "故障排除.txt", KB + "扫拖一体机器人100问.txt"],
                    ),
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-dev-correction-mop-water-level-02",
                "水箱其实没装到位、里面没水",
                [
                    T("user", "拖地不出水，水箱有水。", claims=["记录水箱有水的前提；检查出水量和出水口。"], forbidden=["不要直接判水泵坏。"], state={"case_status": "active", "scope": "mop", "tried_actions": [{"action_id": "check_water_tank", "outcome": "unknown"}], "not_tried_actions": ["clear_water_outlet"]}, behaviors=["先按有水的前提排查。"], sources=[KB + "故障排除.txt"]),
                    T("assistant_fixture", "检查出水口。"),
                    T(
                        "user",
                        "我看错了，水箱其实没装到位，里面没水。",
                        claims=["接受更正：水箱未装到位且无水，不是出水口堵塞。", "先重新安装水箱并加注清水，再看出水是否恢复；不要继续只查出水口。", "不直接判水泵故障。"],
                        forbidden=["不要坚持水箱已装好或有水。", "不要只让用户继续疏通出水口。", "不要建议拆泵。"],
                        state={"case_status": "active", "scope": "mop", "tried_actions": [], "not_tried_actions": ["seat_water_tank", "fill_water_tank", "clear_water_outlet"]},
                        behaviors=["更新前提后再给步骤。", "区分安装问题与堵塞问题。"],
                        sources=[KB + "故障排除.txt", KB + "扫拖一体机器人100问.txt"],
                    ),
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["更正后的前提是否真的改变建议", "是否继续依赖旧前提"],
    ),
    F(
        "fam-dev-ambiguity-recharge-condition",
        "negation_correction_ambiguity",
        "development",
        "回充指代/症状与90%/95%条件歧义",
        "先澄清指代/症状或条件；不把不同条件当同一矛盾；不合并成唯一数值。",
        ["ev-pdf-no-return", "ev-fault-base-power", "ev-fault-recharge-notfound", "ev-guide-95", "ev-guide-90", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-ambiguity-recharge-condition-01",
                "“它回充不行”指代和症状不明",
                [
                    T(
                        "user",
                        "它回充不行，帮我看看。",
                        claims=["“它”指代和“回充不行”的具体症状不明确，先问是找不到充电座、对不准/碰撞，还是充电座灯不亮/不供电。", "在确认现象前不直接给唯一维修结论。"],
                        forbidden=["不要在未确认现象时断定主板或传感器坏。", "不要假定用户说的是某个具体型号。"],
                        state={"case_status": "active", "scope": "recharge"},
                        behaviors=["用一到两个关键问题补齐指代和现象。", "可给一条安全的观察项，但不倾倒全部步骤。"],
                        sources=[KB + "故障排除.txt", KB + "扫地机器人100问.pdf"],
                    )
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-dev-ambiguity-recharge-condition-02",
                "90%与95%是不是同一标准",
                [
                    T(
                        "user",
                        "回充精准度≥95%是唯一标准吗？我看到还有≥90%。",
                        claims=[
                            "选购指南一处写“支持自动回充精准度≥95%”，另一处写“复杂环境下回充精准度≥90%”；两处条件/语境不同，不能当成同一条件下的矛盾或唯一标准。",
                            "应分别说明出处和条件，不用一个数值替代另一个，也不声称是官方实测承诺。",
                        ],
                        forbidden=[
                            "不要把它说成资料自相矛盾、必须二选一。",
                            "不要把它当成具体型号的实测承诺。",
                            "不要编造第三方测试或统一标准。",
                        ],
                        state={},
                        behaviors=["引用两条来源并说明条件差异。", "必要时追问具体型号/使用环境。"],
                        sources=[KB + "选购指南.txt"],
                    )
                ],
                seed_ids=["rs-11-conflict-recharge-accuracy"],
            ),
        ],
        human_focus=["是否把条件差异误判为真冲突", "是否在指代不明时直接下结论"],
    ),
    F(
        "fam-dev-boundary-unknown-model-price",
        "missing_or_out_of_domain",
        "development",
        "未知型号价格/保修的语料覆盖边界",
        "说明语料覆盖不足；不给X99价格；通用保修不冒充官方承诺；不以top-k未命中当不存在证明。",
        ["req-kb-scope", "ev-1002-warranty", "ev-guide-price-range", "ev-pdf-avva-price", "ev-guide-warranty", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-boundary-unknown-model-price-01",
                "Galaxy X99售价和保修",
                [
                    T(
                        "user",
                        "扫地机器人 Galaxy X99 现在售价多少，保修几年？",
                        claims=[
                            "这6份资料没有Galaxy X99的型号价格信息；这是对现有语料的覆盖判断，不能扩展为现实世界不存在，也不是一次检索未命中的证明。",
                            "不能给出X99具体售价；选购指南只有泛化价格区间，PDF只有Avva 30的历史价格例子，不能套用。",
                            "保修只能给一般说明：一般整机1-2年、电池6个月，需以说明书、发票、APP设备信息或品牌官方为准，不是X99官方承诺。",
                        ],
                        forbidden=[
                            "不要报X99的具体价格数字。",
                            "不要把泛化价格区间或其他型号价格当成X99售价。",
                            "不要把通用保修说成X99的官方承诺。",
                            "不要声称知识库里有X99的型号资料。",
                        ],
                        state={},
                        behaviors=["先说明资料不足，再给可核验渠道。", "不堆无关来源凑引用。"],
                        sources=["README.md", KB + "扫地机器人100问2.txt", KB + "选购指南.txt", KB + "扫地机器人100问.pdf"],
                    )
                ],
                seed_ids=["rs-09-unknown-model-price"],
            ),
            V(
                "rsv1-dev-boundary-unknown-model-price-02",
                "X99电池保修是不是6个月",
                [
                    T(
                        "user",
                        "X99的电池保修是不是6个月？给我个确定答案。",
                        claims=[
                            "6个月是资料中的一般电池保修说明，不是X99的型号专属承诺。",
                            "不能从这6份资料确认X99的保修；应查说明书、发票、APP设备信息或品牌官方。",
                            "可以给一般说明，但必须与型号专属结论分开。",
                        ],
                        forbidden=["不要断言X99官方保修就是6个月。", "不要编造X99说明书或官方页面。", "不要因为不能确认就拒绝给任何一般说明。"],
                        state={},
                        behaviors=["区分一般知识与型号专属信息。", "引导用户提供说明书或走官方渠道。"],
                        sources=["README.md", KB + "扫地机器人100问2.txt"],
                    )
                ],
                seed_ids=["rs-09-unknown-model-price"],
            ),
        ],
        human_focus=["是否把语料覆盖说成现实不存在", "是否把通用保修冒充型号承诺"],
    ),
    F(
        "fam-dev-boundary-out-of-domain",
        "missing_or_out_of_domain",
        "development",
        "域外请求与账号能力边界",
        "只处理扫地/扫拖；不提供服装建议；不查账号/订单；不伪装已执行。",
        ["req-kb-scope", "req-no-actions", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-boundary-out-of-domain-01",
                "连衣裙配鞋",
                [
                    T(
                        "user",
                        "我明天穿这件连衣裙，配什么鞋好看？",
                        claims=["说明本客服只处理扫地/扫拖机器人相关问题，不提供服装搭配建议。", "知识库只含6份扫地/扫拖资料，不含服装资料；可引导回机器人问题。"],
                        forbidden=["不要给连衣裙配鞋建议。", "不要编造服装知识库或引用不存在的服装来源。", "不要输出内部检索错误或堆栈。"],
                        state={},
                        behaviors=["简短拒绝并说明范围，不进行无关服装检索。"],
                        sources=["README.md"],
                    )
                ],
                seed_ids=["rs-10-out-of-domain-dress"],
            ),
            V(
                "rsv1-dev-boundary-out-of-domain-02",
                "查账号订单并推荐扫地机",
                [
                    T(
                        "user",
                        "帮我查一下我的账号订单，顺便推荐一款扫地机。",
                        claims=["不能查询账号或订单，因为没有业务动作工具，也不能读取个人数据。", "可以在6份资料范围内给通用选购信息，但需要户型、预算、地面/宠物等条件。", "不声称已经查询订单或看到账号信息。"],
                        forbidden=["不要声称已查订单/账号。", "不要编造个人数据、库存或官方价格。", "不要把通用推荐说成官方购买承诺。"],
                        state={},
                        behaviors=["区分能力边界与可回答部分。", "对选购部分追问必要信息。"],
                        sources=["README.md"],
                    )
                ],
                seed_ids=["rs-10-out-of-domain-dress"],
            ),
        ],
        human_focus=["是否越域给建议", "是否伪装已执行账号操作"],
    ),
    F(
        "fam-dev-boundary-missing-context",
        "missing_or_out_of_domain",
        "development",
        "开不了机/突然不动：缺上下文先追问",
        "先问指示灯、电量、供电、异响/进水等；不假设已试全部步骤；不猜主板。",
        ["ev-fault-no-power", "ev-1002-battery-fault", "ev-1002-power-alarm", "ev-fault-auto-shutdown", "ev-maint-battery-check", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-boundary-missing-context-01",
                "只说开不了机，随后还是不行",
                [
                    T("user", "机器人开不了机。", claims=["先确认指示灯是否亮、是否充过电、充电座/适配器是否通电，以及近期是否跌落或进水。", "可给一条安全观察项，不直接判主板。"], forbidden=["不要在没有上下文时直接给更换主板或拆机结论。"], state={}, behaviors=["用简短追问补齐现象，不一次倾倒全部步骤。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                    T("user", "还是不行。", claims=["继续追问具体观察：指示灯亮不亮、是否有声音、放上充电座是否有反应。", "不把“还是不行”当作已经执行过全部步骤。", "在得到信息前不输出确定性硬件判断。"], forbidden=["不要把“还是不行”当成已试全部步骤。", "不要直接判主板故障。"], state={}, behaviors=["用一到两个关键问题补上下文。", "可给一条安全的供电/充电观察项。"], sources=[KB + "故障排除.txt", KB + "扫地机器人100问2.txt"]),
                ],
                seed_ids=["rs-04-still-bad-no-context"],
            ),
            V(
                "rsv1-dev-boundary-missing-context-02",
                "突然不动、没有提示",
                [
                    T("user", "机器人突然不动了。", claims=["先确认是完全关机、卡住不动，还是能开机但不移动。", "询问电量、是否被异物卡住、是否有报警/异响，再给安全步骤。"], forbidden=["不要直接判断驱动轮或电机损坏。"], state={}, behaviors=["先澄清“不动”的具体含义。"], sources=[KB + "故障排除.txt", KB + "扫地机器人100问2.txt"]),
                    T("user", "就突然停了，没提示。", claims=["继续确认电量/充电状态、轮组是否卡住、机身是否过热，以及是否在潮湿地面工作。", "不假设用户已经检查过轮组或电量。", "在信息不足时不建议拆机。"], forbidden=["不要断言主板或电机故障。", "不要建议自行拆解。"], state={}, behaviors=["给低风险观察项并继续追问。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                ],
                seed_ids=["rs-04-still-bad-no-context"],
            ),
        ],
        human_focus=["是否在缺信息时猜硬件", "是否把模糊回答当已试"],
    ),
    F(
        "fam-dev-fault-process-restart",
        "fault_recovery",
        "development",
        "进程重启后的会话状态恢复与隔离",
        "同session恢复已试事实；新session不继承；重启不是任务成功；故障样本不进QA分母。",
        ["ev-maint-contacts", "ev-fault-manual-return", "req-retrieval-limits", "req-answer-behavior"],
        [
            V(
                "rsv1-dev-fault-process-restart-01",
                "重启后同一会话回忆，另一会话隔离",
                [
                    T("user", "回充失败，我已经清理了充电座周围，也擦了充电触点。", claims=["记录回充失败及已试清障、擦触点。"], forbidden=["不要编造用户没做过的步骤。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}, {"action_id": "clean_contacts", "outcome": "unknown"}]}, behaviors=["建立会话A状态。"], sources=[KB + "维护保养.txt", KB + "故障排除.txt"], session="A"),
                    T("user", "服务重启后，我刚才试过什么？", claims=["恢复会话A后，仍能复述已清理充电座周围、擦拭充电触点。", "不从其他会话拼接事实。"], forbidden=["不要因重启丢失会话A的已试步骤。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}, {"action_id": "clean_contacts", "outcome": "unknown"}]}, behaviors=["同一session恢复后读取原状态，不要求用户重新描述。"], sources=[KB + "维护保养.txt"], session="A"),
                    T("user", "我刚才试过什么？", claims=["新会话B不应知道会话A的已试步骤。", "应说明本会话没有相关记录，请用户补充。"], forbidden=["不要把会话A的回充状态或已试步骤带入会话B。"], state={"case_status": "active", "scope": "recharge", "tried_actions": []}, behaviors=["明确表示本会话没有记录，不从其他session读取任务状态。"], sources=[KB + "维护保养.txt"], session="B"),
                ],
                seed_ids=["rs-13-session-resume-isolation"],
                setup={"kind": "fault_injection", "fault": "process_restart", "steps": ["创建会话A并发送回充失败与已试步骤。", "重启worker/服务进程，保留会话存储。", "用同一session_id恢复会话A并追问已试步骤。", "创建新会话B并追问已试步骤。"]},
            ),
            V(
                "rsv1-dev-fault-process-restart-02",
                "重启后继续排查，不把重启当恢复",
                [
                    T("user", "回充失败，充电座灯不亮，我已经换过插座。", claims=["记录回充失败与已试换插座。"], forbidden=["不要编造其他已试步骤。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "check_power", "outcome": "failed"}]}, behaviors=["建立会话A状态。"], sources=[KB + "故障排除.txt"], session="A"),
                    T("user", "重启后还在吗？我刚才查到哪了？", claims=["恢复会话A后复述已试换插座、当前问题仍是充电座灯不亮。", "说明重启只恢复会话，不等于设备故障已恢复。"], forbidden=["不要把服务重启说成回充已恢复。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "check_power", "outcome": "failed"}]}, behaviors=["区分会话恢复与任务成功。"], sources=[KB + "故障排除.txt"], session="A"),
                    T("user", "我试过什么？", claims=["新会话B不知道会话A的换插座记录。"], forbidden=["不要从会话A继承已试步骤。"], state={"case_status": "active", "scope": "recharge", "tried_actions": []}, behaviors=["说明本会话无记录并请用户补充。"], sources=[KB + "故障排除.txt"], session="B"),
                ],
                seed_ids=["rs-13-session-resume-isolation"],
                setup={"kind": "fault_injection", "fault": "process_restart", "steps": ["创建会话A并记录回充失败与换插座。", "重启worker/服务进程。", "同一session_id恢复并追问进度。", "创建新会话B验证隔离。"]},
            ),
        ],
        human_focus=["重启恢复的是会话还是任务成功", "跨session是否污染"],
    ),
]
