"""场景注册表：场景只来自 ``data/specs/<name>/`` 的 active spec。

仓库不再内置任何业务场景；新建场景由编辑器按需创建，构建统一走
``core.scenarios.generic``。
"""
from __future__ import annotations

from core.agent.contracts import Scenario
from core.observability import latency


def _spec_names() -> list[str]:
    """只读扫描 spec 目录，不 import generic（否则会拉起模型/工具链）。"""
    from core.paths import SPEC_ROOT
    from core.scenarios.ownership import OWNER_CODE, load_state

    if not SPEC_ROOT.is_dir():
        return []
    names: list[str] = []
    for directory in sorted(SPEC_ROOT.iterdir()):
        if not directory.is_dir() or not (directory / "active.json").is_file():
            continue
        state = load_state(directory.name, SPEC_ROOT)
        if state is not None and state.owner == OWNER_CODE:
            continue
        names.append(directory.name)
    return names


def list_scenarios() -> list[str]:
    """已注册场景：仅 spec 构建的场景；默认空。"""
    return _spec_names()


def get_scenario(name: str = "") -> Scenario:
    """按名字构建场景；没有 active spec 就报错。"""
    with latency.span("worker.scenario_lookup", scenario=name):
        names = _spec_names()
    if name in names:
        # First call pays ``core.scenarios.generic`` import; later warm calls
        # are a no-op import but still get a (near-zero) span.
        with latency.span("worker.scenario_module_import", scenario=name):
            from core.scenarios.generic import build_generic_spec_scenario

        return build_generic_spec_scenario(name)
    available = ", ".join(list_scenarios())
    raise ValueError(f"未知场景：{name}（可用：{available or '（空，先从编辑器新建）'}）")
