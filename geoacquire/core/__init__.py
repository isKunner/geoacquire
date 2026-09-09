#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: __init__.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Stable public interfaces for GeoAcquire core components.

from .context import RuntimeContext
from .models import (
    AcquisitionReport,
    AcquireOptions,
    AssetKind,
    AssetSpec,
    AssetStatus,
    DownloadRequest,
    Failure,
    LocalAsset,
    ProductType,
    Region,
    RegionMetadataKey,
    RegionResult,
    validate_product_type,
)
from .pipeline import PipelineRunner
from .source import BaseSource, HTTPSource

__all__ = [
    'AcquisitionReport',
    'AcquireOptions',
    'AssetKind',
    'AssetSpec',
    'AssetStatus',
    'BaseSource',
    'DownloadRequest',
    'Failure',
    'HTTPSource',
    'LocalAsset',
    'ProductType',
    'PipelineRunner',
    'Region',
    'RegionMetadataKey',
    'RegionResult',
    'RuntimeContext',
    'validate_product_type',
]
