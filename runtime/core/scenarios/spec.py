# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.scenario_format.spec`。"""
import sys

from modules.scenario_format import spec as _impl

sys.modules[__name__] = _impl
