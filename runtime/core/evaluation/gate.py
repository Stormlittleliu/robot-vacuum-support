# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.evaluation.gate`。"""
import sys

from modules.evaluation import gate as _impl

sys.modules[__name__] = _impl
