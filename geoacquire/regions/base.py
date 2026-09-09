#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: base.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Abstract region provider contract.

from abc import ABC, abstractmethod

from geoacquire.core.models import Region


class BaseRegionProvider(ABC):
    """Resolve external region inputs into stable Region objects."""

    @abstractmethod
    def get_regions(self) -> dict[str, Region]:
        pass
