# -*- coding: utf-8 -*-
"""Canonical action identity shared by interpretation and state.

The semantic layer and the deterministic reducer must not drift: both import
the same action ids from here.  Patterns are only a *fallback* for explicit
same-family phrases; the reducer still validates the current user quote and the
action/object scope before writing a user fact.
"""
from __future__ import annotations

import re
from typing import Any

#: Action catalogue for the single "recharge failure" flow.  Labels are what the
#: user sees; ids are stable and used for de-duplication and provenance.
#: ``auto_next=False`` means "observe if the user did it, never suggest it".
ACTION_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "clear_dock_obstacles",
        "label": "清理充电座周围障碍物",
        "detail": "清理充电座前方和两侧的杂物、遮挡，确认充电座周围有足够空间。",
        "auto_next": True,
    },
    {
        "id": "clean_contacts",
        "label": "清洁充电触点",
        "detail": "用干燥软布清洁机器人底部充电触点和充电座金属片，确认没有灰尘或氧化。",
        "auto_next": True,
    },
    {
        "id": "clean_sensors",
        "label": "清洁回充传感器",
        "detail": "用干布擦拭机器人回充传感器及充电座红外感应区的灰尘，避免信号受遮挡。",
        "auto_next": True,
    },
    {
        "id": "check_power",
        "label": "检查充电座电源",
        "detail": "确认充电座插头插紧、插座通电，充电座指示灯是否亮起。",
        "auto_next": True,
    },
    {
        "id": "check_auto_return",
        "label": "检查自动回充设置",
        "detail": "查看 APP 是否关闭自动回充、地图中的充电座位置是否被改动；只检查设置，不主动清空地图。",
        "auto_next": True,
    },
    {
        "id": "reseat_dock",
        "label": "重新对准充电座",
        "detail": "把机器人放回充电座，确认对准充电极片并听到提示音或看到充电指示。",
        "auto_next": True,
    },
    {
        # New observation-only identity: "replace the dock" must not be confused
        # with reseating the robot, and must never become an automatic hardware
        # replacement suggestion.
        "id": "replace_dock",
        "label": "更换新的充电座",
        "detail": "用户报告更换了新的充电座/基座；只记录观察事实，不自动建议更换硬件。",
        "auto_next": False,
    },
    {
        "id": "move_dock",
        "label": "挪动原来的充电座",
        "detail": "用户报告挪动/搬动了原来的充电座；只记录观察事实，不等同于更换新座。",
        "auto_next": False,
    },
    {
        "id": "reset_robot",
        "label": "重启机器人",
        "detail": "按说明书长按电源键或通过 APP 重启机器人后再尝试回充；重启不等同于恢复出厂。",
        "auto_next": True,
    },
    {
        "id": "check_adapter",
        "label": "检查电源适配器",
        "detail": "检查适配器与线缆是否松动、破损；只有在资料支持时提及同规格替换，并提醒联系专业检测。",
        "auto_next": True,
    },
    {
        "id": "factory_reset",
        "label": "恢复出厂设置（高风险）",
        "detail": "恢复出厂可能清除地图和设置，不应由客服自动建议；如用户已操作，只记录事实并提醒风险。",
        "auto_next": False,
    },
)
ACTION_BY_ID = {action["id"]: action for action in ACTION_CATALOG}
ACTION_IDS = tuple(action["id"] for action in ACTION_CATALOG)

#: Fallback phrase families.  Keep this small and explicit; semantic proposals
#: are preferred, and the state reducer still checks quote/object scope.
ACTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "clear_dock_obstacles": re.compile(
        r"(?:清|清理|清除|扫|搬|移|挪|整理).{0,12}(?:充电座周围|充电座附近|障碍物|杂物|遮挡)|"
        r"(?:充电座周围|充电座附近|障碍物|杂物|遮挡).{0,12}(?:清|搬|移|挪|整理)|"
        r"清(?:了|理|除)?障"
    ),
    "clean_contacts": re.compile(
        r"(?:擦|清理|清洁|擦拭|洗|抹|弄).{0,8}(?:触点|金属片|充电片)|"
        r"(?:触点|金属片|充电片).{0,8}(?:擦|清理|清洁|擦拭|洗|抹)"
    ),
    "clean_sensors": re.compile(
        r"(?:擦|清理|清洁|擦拭|洗|抹).{0,8}(?:回充传感器|传感器|红外)|"
        r"(?:回充传感器|传感器|红外).{0,8}(?:擦|清理|清洁|擦拭|洗|抹)"
    ),
    "check_power": re.compile(
        r"(?:检查|看了|确认|试了|换).{0,6}(?:电源|插座|插头|通电)|"
        r"(?:电源|插座|插头).{0,6}(?:通电|有电|正常|没问题)"
    ),
    "check_auto_return": re.compile(
        r"(?:检查|查看|关闭|打开|开启|确认).{0,10}(?:自动回充|地图)|"
        r"(?:自动回充|地图).{0,10}(?:关闭|打开|开启|设置|位置)"
    ),
    "reseat_dock": re.compile(
        r"(?:重新|又|再).{0,6}(?:放回|对准|归位|回充|放上)|"
        r"(?:放回|对准|归位).{0,6}(?:充电座|基座|座)"
    ),
    "replace_dock": re.compile(
        r"(?:换|更换|换掉|换成|买|买了|购买).{0,10}(?:新|全新).{0,4}(?:充电座|充电桩|基座|座)|"
        r"(?:新|全新)(?:的)?(?:充电座|充电桩|基座|座)"
    ),
    "move_dock": re.compile(
        r"(?:挪|搬(?!家|完家)|移|移动|挪动|移位).{0,10}(?:充电座|充电桩|基座|旧座|原座|座)|"
        r"(?:充电座|充电桩|基座|旧座|原座).{0,10}(?:挪|搬(?!家|完家)|移|移动|挪动|移位)|"
        r"(?:充电座|基座).{0,6}(?:位置|地方).{0,4}(?:变|换|移|挪)"
    ),
    "reset_robot": re.compile(r"重启(?:了|过|机器人)?|关机.{0,4}(?:再)?开"),
    "factory_reset": re.compile(r"恢复出厂(?:设置)?|重置机器人|恢复默认设置"),
    "check_adapter": re.compile(r"(?:检查|换|试).{0,6}(?:适配器|充电器)|适配器.{0,4}(?:坏|换|正常)"),
}

_DOCK_OBJECT = re.compile(r"充电座|充电桩|基座|旧座|原座|座")
_ROBOT_PLACE = re.compile(
    r"(?:机器人|机器|它).{0,8}(?:放回|放上|对准|归位)|"
    r"(?:放回|放上|对准|归位).{0,8}(?:机器人|机器|充电座|基座|座)"
)
_SERVICE_TERM = r"服务|服务器|后台|会话|session|进程|系统|程序|软件|APP|app|平台|接口|worker"
_ROBOT_TERM = r"机器人|机器|扫地机|扫拖机|设备"
_SERVICE_RESTART = re.compile(
    rf"(?:{_SERVICE_TERM}).{{0,8}}重启|重启.{{0,8}}(?:{_SERVICE_TERM})"
)
_ROBOT_RESTART = re.compile(
    rf"(?:{_ROBOT_TERM}).{{0,8}}重启|重启.{{0,8}}(?:{_ROBOT_TERM})"
)
#: Settings/map evidence is required for the ``check_auto_return`` identity.  A
#: plain "it returned and started charging" is an automatic-return outcome, not
#: evidence that the user inspected a setting.
_AUTO_SETTINGS = re.compile(
    r"(?:检查|查看|确认|关闭|打开|开启|设置|配置|调整|恢复).{0,12}(?:自动回充|地图)|"
    r"(?:自动回充|地图).{0,12}(?:设置|关闭|打开|开启|开关|位置|改动|修改|恢复)|"
    r"(?:自动回充|地图).{0,6}(?:开|关)着|"
    r"(?:APP|app).{0,12}(?:自动回充|地图)"
)

#: Clause-level source scope.  A conditional/other-source clause can govern the
#: following result clause ("如果朋友家…，手动放上去能充").  The reducer may not
#: turn that result into the current user's own observation.  A first-person
#: subject, a contrast conjunction or a sentence boundary starts a new scope.
_SCOPE_SPLIT = re.compile(r"[，。；！？,.;!?]|但(?:是)?|不过|可是|只是|然而")
_SCOPE_CONDITIONAL = re.compile(r"如果|要是|假如|万一|的话")
_SCOPE_OTHER_SOURCE = re.compile(
    r"别人|他人|朋友|家人|客服|售后|网上|论坛|说明书|资料|据说|听说|"
    r"有人说|卖家|商家|视频|帖子里"
)
_SCOPE_OWN = re.compile(r"^\s*我|我家|我的|我这台|我自己的|本人")
_SCOPE_HARD_BOUNDARY = set("。；！？;!?") | {"但", "但是", "不过", "可是", "只是", "然而"}


def scoped_clauses(text: str) -> list[dict[str, Any]]:
    """Split into clauses while carrying conditional/other-source scope.

    Each item has ``text``, ``start``, ``end`` and ``blocked`` (``None`` when
    the clause is a current-user statement).  This is a source-scope guard, not
    a general language-understanding layer.
    """
    value = str(text or "")
    parts: list[dict[str, Any]] = []
    start = 0
    inherited: str | None = None

    def add(raw: str, end: int) -> None:
        nonlocal inherited
        clause = raw.strip()
        if not clause:
            return
        if _SCOPE_OWN.search(clause):
            inherited = None
        local: str | None = None
        if _SCOPE_CONDITIONAL.search(clause):
            local = "hypothetical_or_question"
        elif _SCOPE_OTHER_SOURCE.search(clause):
            local = "other_source_not_user_observation"
        blocked = local or inherited
        clause_start = start + (len(raw) - len(raw.lstrip()))
        parts.append(
            {"text": clause, "start": clause_start, "end": end, "blocked": blocked}
        )
        if local:
            inherited = local

    for match in _SCOPE_SPLIT.finditer(value):
        add(value[start : match.start()], match.start())
        if match.group(0) in _SCOPE_HARD_BOUNDARY:
            inherited = None
        start = match.end()
    add(value[start:], len(value))
    return parts


def clause_scope_at(text: str, position: int) -> str | None:
    """Return the inherited source-scope reason for the clause at ``position``."""
    try:
        point = int(position)
    except (TypeError, ValueError):
        return None
    for part in scoped_clauses(text):
        if int(part["start"]) <= point < int(part["end"]):
            return part["blocked"]
    return None


def display_action_label(action_id: str, label: str | None, evidence: str | None) -> str:
    """Label a persisted action without overstating what the user did.

    Old states may contain ``check_auto_return`` because an earlier interpreter
    mapped autonomous recovery to that identity.  Automatic recovery must not
    render as "checked automatic-return settings"; use a neutral evidence label
    until the stored quote actually supports the settings identity.
    """
    fallback = str(label or (ACTION_BY_ID.get(action_id) or {}).get("label") or action_id)
    if action_id == "reseat_dock":
        # The shared identity covers both "随手放上" and "特意对准".  The
        # catalogue/detail keeps the stronger "重新对准" instruction, but a
        # persisted user fact may only support the neutral placement label.
        return "手动放回充电座"
    if action_id == "check_auto_return" and not _AUTO_SETTINGS.search(str(evidence or "")):
        return "自动回充（用户原话）"
    return fallback


def service_restart_only(text: str) -> bool:
    """True when "restart" clearly refers to a service/session, not the robot."""
    value = str(text or "")
    return bool(_SERVICE_RESTART.search(value)) and not bool(_ROBOT_RESTART.search(value))


def claim_rejection(
    action_id: str,
    quote: str,
    text: str,
    *,
    require_execution: bool = False,
) -> str | None:
    """Return a rejection reason for a claimed user action, or ``None``.

    This is the shared provenance/scope gate used by both the semantic
    normalizer and the state reducer.  It does not decide whether the user
    *actually* did the action; it only rejects claims that cannot be tied to the
    current message and the action's object.

    ``require_execution`` is for executed-action claims.  Automatic recovery is
    not evidence that the user checked the auto-return setting, so that identity
    is rejected for executed claims unless settings/map wording is present.
    A not-tried claim can still use the identity as a suggested next check.
    """
    if action_id not in ACTION_BY_ID:
        return "unknown_action"
    value = str(text or "")
    evidence = str(quote or "")
    if not evidence:
        return "missing_evidence_quote"
    if evidence not in value:
        return "quote_not_in_current_message"
    occurrences = [match.start() for match in re.finditer(re.escape(evidence), value)]
    if occurrences and all(clause_scope_at(value, point) for point in occurrences):
        return "source_scope_not_current_user"
    # If the quote clearly matches a different action family, the claimed
    # action id is not supported by the user text.  This catches "I replaced the
    # dock" being labelled clean_contacts while still allowing paraphrases that
    # no fallback pattern knows.
    matched_actions = {
        candidate
        for candidate, pattern in ACTION_PATTERNS.items()
        if pattern.search(evidence)
    }
    if matched_actions and action_id not in matched_actions:
        return "quote_action_mismatch"
    if action_id == "reset_robot":
        if _SERVICE_RESTART.search(evidence):
            return "service_restart_not_device_restart"
        if service_restart_only(value) and not _ROBOT_RESTART.search(evidence):
            return "service_restart_not_device_restart"
    if (
        require_execution
        and action_id == "check_auto_return"
        and not _AUTO_SETTINGS.search(evidence)
    ):
        return "auto_return_settings_not_evidenced"
    if action_id in {"replace_dock", "move_dock"} and not _DOCK_OBJECT.search(evidence + value):
        return "dock_object_out_of_scope"
    if action_id == "reseat_dock" and not (
        _DOCK_OBJECT.search(evidence + value) or _ROBOT_PLACE.search(evidence + value)
    ):
        return "reseat_object_out_of_scope"
    return None
