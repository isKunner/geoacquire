#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: context.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Explicit runtime services injected into Sources and post-processors.

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from geoacquire.core.models import Region, RegionResult
from geoacquire.services.http_download import HTTPDownloadService

if TYPE_CHECKING:
    from geoacquire.services.run_reporting import RunReportingService


@dataclass(frozen=True)
class RuntimeContext:
    """Shared runtime services. Services never select or inspect concrete Sources."""

    workspace_dir: str
    http: HTTPDownloadService
    # Pipeline injects an enqueue-only callback for the current Source. Direct
    # source.acquire() calls work unchanged when no postprocessing is requested.
    on_region_ready: Callable[[str, RegionResult], None] | None = None
    reporting: 'RunReportingService | None' = None
    pipeline_name: str | None = None

    def resolve_path(self, path: str) -> str:
        return os.path.abspath(path if os.path.isabs(path) else os.path.join(self.workspace_dir, path))

    def resolve_acquire_output(
        self,
        output_dir: str | None,
        regions: dict[str, Region],
    ) -> str:
        """Resolve the one download root without inventing a data directory.

        An explicit value always wins. With null, target-backed Regions use
        their common parent directory; bbox-only Regions use workspace/output.
        Targets from multiple parent directories require an explicit root.
        """
        if output_dir is not None:
            return self.resolve_path(output_dir)

        target_parents = {
            os.path.dirname(os.path.abspath(region.target_path))
            for region in regions.values()
            if region.target_path
        }
        target_count = sum(
            bool(region.target_path)
            for region in regions.values()
        )
        if not target_parents:
            return self.resolve_path('./output')
        if len(target_parents) == 1 and target_count == len(regions):
            return target_parents.pop()
        raise ValueError(
            'acquire.output_dir is null, but Regions do not share one target directory; '
            'set acquire.output_dir explicitly'
        )
