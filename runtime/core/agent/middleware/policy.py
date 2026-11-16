# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.middleware.policy`。"""
import sys

from modules.runtime.middleware import policy as _impl

sys.modules[__name__] = _impl
