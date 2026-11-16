# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.checkpoint`。"""
import sys

from modules.runtime import checkpoint as _impl

sys.modules[__name__] = _impl
