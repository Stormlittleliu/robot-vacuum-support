# -*- coding: utf-8 -*-
"""Holdout families (8 new families x 2 variants = 16 cases).

These families were not used for the v0 debug cases.  They may share generic
knowledge documents, but the scenario + oracle pairs are new and are frozen
after this build; no holdout turns are read while tuning wording.
"""
from __future__ import annotations

from seed_helpers import F, T, V

KB = "assets/knowledge/"

FAMILIES = [
    F(
        "fam-hold-direct-charge-heat",
        "direct_qa",
        "holdout",
        "充电座发热/异味的安全优先处理",
        "异常发热/焦味先停止充电、通风、查原装适配器/线缆；不拆机；鼓包/火花走官方售后。",
        ["ev-1002-charge-heat", "ev-fault-base-heat", "ev-fault-spark", "ev-pdf-overcharge", "ev-guide-base-waterproof", "ev-maint-power-cord", "ev-maint-no-diy"],
        [
            V(
                "rsv1-hold-direct-charge-heat-01",
                "充电座很烫还有一点异味",
                [
                    T(
                        "user",
                        "机器人充电时充电座很烫，还有一点异味，正常吗？",
                        claims=[
                            "异常发热并伴随异味不应当作正常现象继续充电；先停止充电并保持通风。",
                            "检查是否使用原装充电座/适配器、充电环境是否密闭、线缆是否破损。",
                            "若出现火花、焦味或电池鼓包，应停止使用并联系官方售后，不自行拆修。",
                        ],
                        forbidden=[
                            "不要说“发热正常，继续充”。",
                            "不要教用户拆充电座或短接。",
                            "不要建议使用非原装适配器。",
                            "不要承诺官方检测结果。",
                        ],
                        state={"case_status": "active", "scope": "recharge"},
                        behaviors=["安全优先，先让用户停止充电。", "追问是否有火花、焦味、鼓包或线缆破损。"],
                        sources=[KB + "扫地机器人100问2.txt", KB + "故障排除.txt", KB + "选购指南.txt", KB + "维护保养.txt"],
                    )
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-hold-direct-charge-heat-02",
                "充电时闻到焦味，该先拔电吗",
                [
                    T(
                        "user",
                        "充电时闻到焦味，充电座摸起来很热，我该先拔电吗？",
                        claims=[
                            "应先停止充电并断开电源，保持通风，不要再继续充电。",
                            "检查充电座/适配器/线缆是否为原装、是否破损；若有火花或电池鼓包，停止使用并联系官方售后。",
                            "不提供拆机、短接或内部维修步骤。",
                        ],
                        forbidden=[
                            "不要让用户继续观察或继续充电。",
                            "不要教拆机或更换内部元件。",
                            "不要保证“只是正常发热”。",
                        ],
                        state={},
                        behaviors=["给出立即安全动作。", "区分普通温热与异常过热/焦味。"],
                        sources=[KB + "故障排除.txt", KB + "扫地机器人100问2.txt", KB + "维护保养.txt"],
                    )
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["安全顺序是否正确", "是否越界教维修"],
    ),
    F(
        "fam-hold-direct-edge-brush-wear",
        "direct_qa",
        "holdout",
        "边刷磨损判据与资料周期差异",
        "按刷毛<1cm/变形/断裂判断；周期资料有2-3月与6-12月差异，不写成唯一周期。",
        ["ev-1002-edge-brush", "ev-maint-edge-brush", "ev-pdf-edge-brush", "ev-pdf-edge-brush-life"],
        [
            V(
                "rsv1-hold-direct-edge-brush-wear-01",
                "边刷磨到多短要换",
                [
                    T(
                        "user",
                        "边刷磨到多短要换？用了两个月，刷毛有点弯。",
                        claims=[
                            "按磨损判据：刷毛磨损至小于1cm、变形或断裂时应更换；刷毛弯不一定等于还能用。",
                            "资料对更换周期说法不同：维护保养写一般2-3个月，PDF写6-12个月；不应把其中一个写成唯一周期。",
                            "建议使用原装边刷，并结合边角清扫效果和实际磨损判断。",
                        ],
                        forbidden=["不要只给一个绝对更换周期。", "不要说刷毛弯就一定是电机坏。", "不要建议非原装边刷。"],
                        state={},
                        behaviors=["给出磨损判据，并说明周期资料差异。", "追问实际刷毛长度和边角清扫效果。"],
                        sources=[KB + "维护保养.txt", KB + "扫地机器人100问2.txt", KB + "扫地机器人100问.pdf"],
                    )
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-hold-direct-edge-brush-wear-02",
                "边刷用了半年还在转，需要换吗",
                [
                    T(
                        "user",
                        "边刷用了半年，还在转，需要换吗？",
                        claims=[
                            "能旋转不等于不需要更换；要看刷毛是否小于1cm、变形/断裂，以及边角是否扫不干净。",
                            "周期资料不一致：维护保养写2-3个月，PDF写6-12个月；应以实际磨损和清扫效果为主，不把单一周期当标准。",
                            "需要更换时使用原装边刷。",
                        ],
                        forbidden=["不要只因为能转就说不用换。", "不要强行给唯一月份。", "不要编造官方更换周期。"],
                        state={},
                        behaviors=["按条件回答，不按单一时间下结论。", "说明资料周期差异。"],
                        sources=[KB + "维护保养.txt", KB + "扫地机器人100问.pdf", KB + "扫地机器人100问2.txt"],
                    )
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["是否把周期差异误写成唯一标准", "是否只看旋转不看磨损"],
    ),
    F(
        "fam-hold-multi-base-moved-remap",
        "multi_turn",
        "holdout",
        "搬家/挪动充电座后的回充与地图",
        "充电座位置变动需重新摆放/更新地图/检查红外；不重复只清障；不判主板。",
        ["ev-1002-recharge-space", "ev-1002-auto-return", "ev-fault-recharge-notfound", "ev-maint-base-level", "ev-pdf-no-return", "ev-guide-base-position"],
        [
            V(
                "rsv1-hold-multi-base-moved-remap-01",
                "搬家后回充失败，充电座已换位置",
                [
                    T("user", "搬家后机器人回充总失败，充电座放在新位置。", claims=["记录充电座位置变动；检查摆放空间、红外/传感器和APP地图中的充电座位置。", "不直接判主板或传感器硬件损坏。"], forbidden=["不要把搬家后的回充失败直接归因硬件。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["check_base_placement", "clean_sensors", "check_auto_return"]}, behaviors=["先确认充电座位置/空间和地图是否更新。"], sources=[KB + "故障排除.txt", KB + "扫地机器人100问2.txt", KB + "维护保养.txt"]),
                    T(
                        "user",
                        "我把充电座挪到电视柜旁边了，还是找不到。",
                        claims=[
                            "充电座位置变动可能导致机器人找不到；确认前方/左右无遮挡，充电座摆放平整，并更新/确认地图中的充电座位置。",
                            "检查回充传感器和充电座红外感应区是否积灰，可手动放回测试。",
                            "不重复只让用户清障；不直接判主板。",
                        ],
                        forbidden=["不要忽略充电座已挪动这一事实。", "不要只说再清一次障碍物。", "不要建议拆机。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}, {"action_id": "reseat_dock", "outcome": "unknown"}], "not_tried_actions": ["clean_sensors", "check_auto_return"]},
                        behaviors=["把充电座位置变动作为关键线索。", "给出摆放/地图/传感器下一步。"],
                        sources=[KB + "扫地机器人100问2.txt", KB + "维护保养.txt", KB + "扫地机器人100问.pdf"],
                    ),
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-hold-multi-base-moved-remap-02",
                "充电座前方有柜子，挪开后仍失败",
                [
                    T("user", "换了房间，机器人回充失败。", claims=["记录换房间/位置变动；检查充电座空间、供电和地图。"], forbidden=["不要直接判硬件。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["check_base_placement", "check_power"]}, behaviors=["先确认位置变动和基础供电。"], sources=[KB + "故障排除.txt", KB + "扫地机器人100问2.txt"]),
                    T(
                        "user",
                        "充电座前方有柜子，挪开后还是不行。",
                        claims=[
                            "确认已挪开遮挡，不再重复清障；下一步检查充电座供电/指示灯、红外/传感器和地图位置。",
                            "可手动放回测试，区分充电座信号与机器人回充传感器。",
                            "仍失败再考虑进一步检测或官方售后，不拆机。",
                        ],
                        forbidden=["不要重复只建议挪开柜子。", "不要忽略已清障。", "不要直接判主板。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}], "not_tried_actions": ["check_power", "clean_sensors", "check_auto_return"]},
                        behaviors=["从已清障推进到供电/传感器/地图。", "必要时追问充电座指示灯。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫地机器人100问2.txt"],
                    ),
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["位置变动线索是否被使用", "是否重复清障"],
    ),
    F(
        "fam-hold-multi-switch-to-suction",
        "multi_turn",
        "holdout",
        "回充话题中切换到吸力下降",
        "切换至吸力主题查滤网/风道/主刷；保留回充任务；不把吸力归因回充传感器。",
        ["ev-fault-suction", "ev-maint-suction", "ev-1002-suction", "ev-fault-recharge-notfound", "ev-maint-no-diy"],
        [
            V(
                "rsv1-hold-multi-switch-to-suction-01",
                "回充失败后转问吸力变小",
                [
                    T("user", "回充失败，充电座周围已经清理过了，灯也正常。", claims=["记录回充失败和已清障；保留回充任务。"], forbidden=["不要重复清障作为唯一建议。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}], "not_tried_actions": ["check_power"]}, behaviors=["保留旧回充任务。"], sources=[KB + "故障排除.txt"]),
                    T(
                        "user",
                        "先不管回充，吸力也变小了，怎么回事？",
                        claims=["切换到吸力下降主题，检查尘盒、滤网、风道、主刷和吸口。", "不把吸力下降归因于回充传感器或充电座；保留旧回充任务。"],
                        forbidden=["不要用回充传感器解释吸力下降。", "不要建议拆电机。", "不要丢弃旧回充问题。"],
                        state={"case_status": "active", "scope": "suction", "tried_actions": [], "not_tried_actions": ["clean_filter", "check_air_duct", "clean_main_brush"]},
                        behaviors=["来源作用域切到吸力/耗材。", "旧回充任务保留。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫地机器人100问2.txt"],
                    ),
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-hold-multi-switch-to-suction-02",
                "回充失败后又说吸力下降、尘盒满",
                [
                    T("user", "回充失败，充电座灯正常。", claims=["记录回充失败与充电座灯正常；检查传感器/摆放/地图。"], forbidden=["不要直接判硬件。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "check_power", "outcome": "success"}], "not_tried_actions": ["clean_sensors", "check_auto_return"]}, behaviors=["保留回充任务。"], sources=[KB + "故障排除.txt"]),
                    T(
                        "user",
                        "还有，吸力下降得厉害，尘盒满了。",
                        claims=["切换到吸力主题：先清空尘盒，再检查滤网、风道、主刷和吸口。", "保留回充任务；不把吸力问题归因于充电座。"],
                        forbidden=["不要把吸力下降说成回充故障。", "不要只清尘盒就结束。", "不要建议拆电机。"],
                        state={"case_status": "active", "scope": "suction", "tried_actions": [], "not_tried_actions": ["empty_dust_box", "clean_filter", "check_air_duct"]},
                        behaviors=["先处理用户新报的尘盒满，再推进滤网/风道。", "旧回充任务保留。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["主题切换后是否串域", "旧回充任务是否保留"],
    ),
    F(
        "fam-hold-multi-docking-alignment",
        "multi_turn",
        "holdout",
        "能到充电座前但对不准/碰撞",
        "区分找不到与对不准；查摆放平整、红外/触点、手动放回；不判主板。",
        ["ev-fault-recharge-notfound", "ev-fault-collide-base", "ev-fault-manual-return", "ev-maint-ir", "ev-pdf-no-return", "ev-maint-no-diy"],
        [
            V(
                "rsv1-hold-multi-docking-alignment-01",
                "能到充电座前但来回进退",
                [
                    T("user", "机器人能回到充电座前面，但总是对不准，来回进退。", claims=["把现象限定为“能到座前但对不准”，不是找不到充电座；检查充电座摆放平整、周围障碍、红外/触点和手动放回。"], forbidden=["不要把它说成找不到充电座。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["check_base_placement", "clean_sensors", "reseat_dock"]}, behaviors=["先区分现象，再给对不准的检查项。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                    T(
                        "user",
                        "充电座在角落，地毯边缘有点翘，还是对不准。",
                        claims=["检查充电座是否平整、周围/地毯边缘是否干扰，调整摆放并清理遮挡。", "清洁回充传感器/充电座红外区和触点，手动放回测试；不直接判主板。"],
                        forbidden=["不要忽略地毯边缘干扰。", "不要建议拆机。", "不要只说再试一次。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clear_dock_obstacles", "outcome": "success"}, {"action_id": "check_base_placement", "outcome": "failed"}], "not_tried_actions": ["clean_sensors", "reseat_dock"]},
                        behaviors=["把摆放/地毯边缘作为线索。", "给出清洁与手动放回的下一步。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt", KB + "扫地机器人100问.pdf"],
                    ),
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-hold-multi-docking-alignment-02",
                "回充时撞一下充电座再退开",
                [
                    T("user", "回充时机器人会撞一下充电座再退开。", claims=["把现象识别为对准/识别问题；检查充电座摆放平整、传感器/红外和触点，尝试手动放回。"], forbidden=["不要直接判回充传感器硬件故障。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["check_base_placement", "clean_sensors", "reseat_dock"]}, behaviors=["先给摆放与传感器检查，不拆机。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                    T(
                        "user",
                        "擦了传感器，也把底座摆平了，还是撞。",
                        claims=["确认已擦传感器、已调平底座，不再重复这两步；下一步手动放回/检查充电座信号和触点。", "仍失败可联系官方售后检测，不自行拆机。"],
                        forbidden=["不要重复擦传感器/调平作为唯一建议。", "不要断定主板坏。", "不要提供拆机步骤。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "clean_sensors", "outcome": "success"}, {"action_id": "check_base_placement", "outcome": "success"}], "not_tried_actions": ["reseat_dock", "clean_contacts"]},
                        behaviors=["从已试步骤推进到手动放回/触点/信号。", "安全边界清楚。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["是否区分找不到与对不准", "是否重复已试步骤"],
    ),
    F(
        "fam-hold-boundary-renovation-debris",
        "missing_or_out_of_domain",
        "holdout",
        "装修后开荒：能否直接用机器人",
        "先人工清水泥渣/沙粒/尖锐杂物；不直接用；避免刮伤/堵塞；开荒后再用。",
        ["ev-sweepmop-renovation", "ev-maint-renovation", "ev-fault-floor-scratch", "ev-maint-no-diy", "req-answer-behavior"],
        [
            V(
                "rsv1-hold-boundary-renovation-debris-01",
                "装修完有水泥渣和沙粒，能否开荒",
                [
                    T(
                        "user",
                        "家里刚装修完，地上有水泥渣和沙粒，能直接让扫拖机器人开荒吗？",
                        claims=["不建议直接用机器人开荒；先人工清理水泥渣、沙粒和尖锐杂物。", "否则可能刮伤地面/拖布、堵塞滤网或卡死机件；开荒后再用机器人常规清洁。", "不承诺机器人能处理建筑垃圾。"],
                        forbidden=["不要说可以直接用。", "不要建议只加大吸力硬扫。", "不要忽略沙粒划伤风险。", "不要编造商用/工业清洁能力。"],
                        state={},
                        behaviors=["先保护地面和机器，再谈清扫。", "追问地面材质和垃圾类型。"],
                        sources=[KB + "扫拖一体机器人100问.txt", KB + "维护保养.txt", KB + "故障排除.txt"],
                    )
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-hold-boundary-renovation-debris-02",
                "装修后有细灰和沙粒，想直接拖地",
                [
                    T(
                        "user",
                        "装修后想用机器人拖地，地面有细小灰尘和沙粒，可以吗？",
                        claims=["先手动吸走/清理沙粒和硬物，检查拖布无砂粒后再拖；不要直接用湿拖碾沙粒。", "装修后的水泥渣/尖锐杂物需人工开荒，机器人只适合后续常规清洁。", "若已出现划痕，先停用检查拖布/轮组。"],
                        forbidden=["不要直接建议湿拖。", "不要说机器人能处理建筑垃圾。", "不要承诺不伤地板。"],
                        state={},
                        behaviors=["区分细灰与磨蚀性沙粒。", "给出“先干清、再湿拖”的安全顺序。"],
                        sources=[KB + "扫拖一体机器人100问.txt", KB + "维护保养.txt", KB + "故障排除.txt"],
                    )
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["是否把开荒当常规清洁", "是否忽略磨蚀性沙粒"],
    ),
    F(
        "fam-hold-negation-battery-vs-base",
        "negation_correction_ambiguity",
        "holdout",
        "电池与充电座更换对象被说反",
        "以最后明确更正为准；区分换电池/换座；不重复被更正掉的建议；不判主板。",
        ["ev-pdf-battery-life", "ev-fault-base-power", "ev-fault-battery-wont-charge", "ev-maint-battery-check", "ev-maint-no-diy", "ev-1002-battery-fault"],
        [
            V(
                "rsv1-hold-negation-battery-vs-base-01",
                "先说换了新电池，后更正是换了充电座",
                [
                    T("user", "回充失败，我已经换了新电池。", claims=["记录用户声称已换电池；继续了解充电现象。"], forbidden=["不要编造电池品牌/价格。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "replace_battery", "outcome": "unknown"}], "not_tried_actions": ["check_power", "clean_contacts"]}, behaviors=["把换电池作为待确认事实。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                    T("assistant_fixture", "那检查充电座供电和触点。"),
                    T(
                        "user",
                        "更正：没换电池，是换了充电座。",
                        claims=["接受更正：充电座已换，电池未换；不继续按已换电池回答。", "检查充电座供电、触点和回充传感器；不重复建议换电池。", "不因换过座就断定主板故障。"],
                        forbidden=["不要继续声称用户换了电池。", "不要再次把换电池作为首要建议。", "不要建议拆机。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "replace_dock", "outcome": "unknown"}], "not_tried_actions": ["check_power", "clean_contacts"]},
                        behaviors=["以最后一次明确更正为准。", "把建议转向供电/触点/传感器。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=[],
            ),
            V(
                "rsv1-hold-negation-battery-vs-base-02",
                "先说换了充电座，后更正是换了电池",
                [
                    T("user", "机器人充不进电，我换了充电座还是不行。", claims=["记录已换充电座；检查供电、触点和电池状态。"], forbidden=["不要直接判主板。"], state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "replace_dock", "outcome": "unknown"}], "not_tried_actions": ["check_power", "clean_contacts"]}, behaviors=["保留换座事实。"], sources=[KB + "故障排除.txt", KB + "维护保养.txt"]),
                    T("assistant_fixture", "再看看电池和触点。"),
                    T(
                        "user",
                        "我说错了，换的是电池，不是充电座。",
                        claims=["接受更正：电池已换，充电座未换；不继续按已换座回答。", "检查充电座供电、电源线和触点；不重复建议换电池。", "不直接判主板。"],
                        forbidden=["不要继续声称用户换了充电座。", "不要重复建议换电池。", "不要建议拆机。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [{"action_id": "replace_battery", "outcome": "unknown"}], "not_tried_actions": ["check_power", "clean_contacts"]},
                        behaviors=["区分电池与充电座两个对象。", "按更正后的对象推进。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=[],
            ),
        ],
        human_focus=["电池/充电座对象是否被混淆", "更正是否覆盖旧事实"],
    ),
    F(
        "fam-hold-fault-empty-retrieval",
        "fault_recovery",
        "holdout",
        "空检索注入：空结果不等于资料不存在",
        "明确本轮检索为空/失败；不编来源；可重试；重试后用真实来源；故障样本不进QA分母。",
        ["ev-fault-mop-no-water", "ev-maint-mop-outlet", "req-retrieval-limits", "req-answer-behavior"],
        [
            V(
                "rsv1-hold-fault-empty-retrieval-01",
                "拖地不出水时检索返回空",
                [
                    T(
                        "user",
                        "拖地不出水，水箱有水，帮我看看。",
                        claims=["如果本轮检索返回空，应明确说明本轮没有检索到可用片段，不能把空结果说成“知识库没有这个问题”。", "不编造来源、片段或确定性维修结论。"],
                        forbidden=["不要把空检索伪装成资料不存在。", "不要编造来源文件名或引用。", "不要在没有依据时断言水泵损坏。"],
                        state={"case_status": "active", "scope": "mop"},
                        behaviors=["区分空检索与知识库无资料。", "保留用户问题，可建议重试或补充信息。"],
                        sources=[],
                    ),
                    T(
                        "user",
                        "那你再查一次。",
                        claims=["重试后若命中资料，按真实来源给出水箱、出水量、出水口/出水管检查。", "不因为第一次空结果就声称资料不存在。"],
                        forbidden=["不要把第一次空检索结果当成资料缺失的证明。", "不要编造重试结果。"],
                        state={"case_status": "active", "scope": "mop", "tried_actions": [], "not_tried_actions": ["check_water_tank", "adjust_water_volume", "clear_water_outlet"]},
                        behaviors=["说明已重试，并只引用本轮真实命中。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=[],
                setup={"kind": "fault_injection", "fault": "empty_retrieval", "steps": ["对“拖地不出水”查询注入一次空检索结果。", "保留用户问题与已试事实。", "重试检索并返回真实命中。", "记录空结果与重试结果。"]},
            ),
            V(
                "rsv1-hold-fault-empty-retrieval-02",
                "回充失败时检索返回空",
                [
                    T(
                        "user",
                        "回充失败，充电座灯不亮。",
                        claims=["本轮检索为空时，明确说明没有检索到可用片段，不把空结果说成资料不存在。", "不编造充电座/主板结论或来源。"],
                        forbidden=["不要编造来源或引用。", "不要因为空检索就断言主板/充电座损坏。", "不要把空检索说成“没有这个问题”。"],
                        state={"case_status": "active", "scope": "recharge"},
                        behaviors=["说明空检索并保留问题。", "可建议重试或补充信息，不输出确定性维修结论。"],
                        sources=[],
                    ),
                    T(
                        "user",
                        "重试一下。",
                        claims=["重试命中后，按真实来源检查电源线、插座、适配器和充电座指示灯。", "不把第一次空检索当作资料缺失证明。"],
                        forbidden=["不要编造重试命中内容。", "不要建议拆充电座。"],
                        state={"case_status": "active", "scope": "recharge", "tried_actions": [], "not_tried_actions": ["check_power", "check_adapter"]},
                        behaviors=["说明重试结果，并只引用真实来源。"],
                        sources=[KB + "故障排除.txt", KB + "维护保养.txt"],
                    ),
                ],
                seed_ids=[],
                setup={"kind": "fault_injection", "fault": "empty_retrieval", "steps": ["对“回充失败/灯不亮”查询注入一次空检索结果。", "保留用户问题。", "重试检索并返回真实命中。", "记录空结果与重试结果。"]},
            ),
        ],
        human_focus=["是否把空检索当资料不存在", "重试后是否只引用真实命中"],
    ),
]
