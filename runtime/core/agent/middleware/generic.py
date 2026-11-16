# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.middleware.generic`。"""
import sys

from modules.runtime.middleware import generic as _impl

sys.modules[__name__] = _impl
