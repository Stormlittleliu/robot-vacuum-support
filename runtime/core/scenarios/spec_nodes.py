# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.scenario_format.spec_nodes`。"""
import sys

from modules.scenario_format import spec_nodes as _impl

sys.modules[__name__] = _impl
