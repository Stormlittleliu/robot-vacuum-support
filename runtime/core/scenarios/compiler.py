# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.compiler`。"""
import sys

from modules.runtime import compiler as _impl

sys.modules[__name__] = _impl
