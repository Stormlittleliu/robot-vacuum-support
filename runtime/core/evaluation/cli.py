# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.diagnosis.cli`。"""
import sys

from modules.diagnosis import cli as _impl

sys.modules[__name__] = _impl
