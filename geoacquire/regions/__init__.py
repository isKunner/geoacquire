#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: __init__.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Region provider public interfaces.

from .base import BaseRegionProvider
from .bounds import BoundsRegionProvider

__all__ = ['BaseRegionProvider', 'BoundsRegionProvider']
