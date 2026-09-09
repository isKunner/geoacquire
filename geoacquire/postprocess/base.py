#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: base.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Base contract for target-oriented processing after acquisition.

from abc import ABC, abstractmethod

from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import AcquisitionReport, AssetSpec, LocalAsset, Region


class BasePostprocessor(ABC):
    """Transform ready local assets, typically using a target raster grid.

    Pipeline passes a single Region and its complete acquisition report per task.
    Steps for one Region run in order. With postprocess_workers > 1 the SAME
    processor instance can receive concurrent calls for different Regions, so
    keep work state local (or protect shared state). No waiting for downloads here.
    """

    def validate_asset_flow(
        self,
        available: frozenset[AssetSpec] | None,
    ) -> frozenset[AssetSpec] | None:
        """Validate/update a declared asset contract; unknown custom flows pass through."""
        return available

    def expected_output(
        self,
        region: Region,
        context: RuntimeContext,
        default_output_dir: str | None,
    ) -> LocalAsset | None:
        """Describe this step's final file, or None when it cannot be inferred.

        The method performs no filesystem access. Pipeline batches existence
        checks by directory before acquisition, avoiding one stat call per
        Region and preventing raw downloads when a final result already exists.
        """
        return None

    @abstractmethod
    def process(
        self,
        regions: dict[str, Region],
        report: AcquisitionReport,
        context: RuntimeContext,
    ) -> AcquisitionReport:
        pass
