# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.pipeline`。"""
import sys

from modules.runtime import pipeline as _impl

sys.modules[__name__] = _impl
