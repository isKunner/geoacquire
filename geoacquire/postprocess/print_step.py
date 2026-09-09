#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: print_step.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Pass-through post-processor used to verify the configured lifecycle.

from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import AcquisitionReport, Region

from .base import BasePostprocessor


class PrintPostprocessor(BasePostprocessor):
    """Print acquired assets and target raster paths, then return the report unchanged."""

    def __init__(self, message: str = 'Post-processing placeholder'):
        self.message = message

    def process(
        self,
        regions: dict[str, Region],
        report: AcquisitionReport,
        context: RuntimeContext,
    ) -> AcquisitionReport:
        print(f'[postprocess] {self.message}')
        for region_id, result in report.regions.items():
            target = regions[region_id].target_path
            print(
                f'[postprocess] region={region_id} target={target or "none"} '
                f'assets={result.asset_count} failed={len(result.failed)}'
            )
        return report
