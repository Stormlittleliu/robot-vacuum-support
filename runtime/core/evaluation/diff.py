# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.diagnosis.diff`。"""
import sys

from modules.diagnosis import diff as _impl

sys.modules[__name__] = _impl
