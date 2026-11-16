# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.runtime.policy_meta`。"""
import sys

from modules.runtime import policy_meta as _impl

sys.modules[__name__] = _impl
