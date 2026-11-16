# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.contracts.agent`。"""
import sys

from modules.contracts import agent as _impl

sys.modules[__name__] = _impl
