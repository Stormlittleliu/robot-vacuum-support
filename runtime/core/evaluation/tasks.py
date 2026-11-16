# -*- coding: utf-8 -*-
"""兼容 shim：实现见 :mod:`modules.evaluation.tasks`。"""
import sys

from modules.evaluation import tasks as _impl

sys.modules[__name__] = _impl
