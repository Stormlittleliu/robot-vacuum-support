# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.diagnosis.repair`。"""
import sys

from modules.diagnosis import repair as _impl

sys.modules[__name__] = _impl
