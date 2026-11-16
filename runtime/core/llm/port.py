# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.contracts.model`。"""
import sys

from modules.contracts import model as _impl

sys.modules[__name__] = _impl
