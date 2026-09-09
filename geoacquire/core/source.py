#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: source.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Source lifecycle contracts and the reusable URL-based acquisition template.

from abc import ABC, abstractmethod
from collections.abc import Iterable

from .acquisition import AcquisitionProgress
from .context import RuntimeContext
from .models import AcquisitionReport, AcquireOptions, AssetSpec, DownloadRequest, Region


class BaseSource(ABC):
    """A complete adapter from Regions to usable local assets."""

    OUTPUT_SPECS: frozenset[AssetSpec] | None = None

    @property
    def output_specs(self) -> frozenset[AssetSpec] | None:
        """Configured output contract, or None when a custom Source cannot declare it."""
        return self.OUTPUT_SPECS

    @abstractmethod
    def acquire(
        self,
        regions: dict[str, Region],
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        pass


class HTTPSource(BaseSource):
    """Template for Sources whose transfer step is ordinary HTTP file download."""

    @abstractmethod
    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        """Yield transfer descriptions lazily instead of building one unbounded list."""
        pass

    def materialize(
        self,
        report: AcquisitionReport,
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        """Convert one downloaded file in its HTTP worker; identity by default.

        The report contains ONE physical file. Implementations must use local
        per-call state: different files may call this method concurrently.
        Return only after outputs are closed. Exceptions become file failures.
        """
        return report

    def reuse_existing(
        self,
        request: DownloadRequest,
        output_dir: str,
        existing_filenames: frozenset[str],
        options: AcquireOptions,
    ) -> AcquisitionReport | None:
        """Return complete derived outputs before transfer, or None to acquire.

        The ordinary downloader already reuses an existing request filename.
        Sources override this only when their usable result has a different
        filename, such as JPG -> GeoTIFF or LAZ -> DSM/DTM.
        """
        return None

    def plan_requests(
        self,
        regions: dict[str, Region],
        progress: AcquisitionProgress,
    ) -> Iterable[DownloadRequest]:
        """Default: plan each Region lazily and explicitly close its file list.

        Batch planners can override this hook to retain shared WESM queries.
        The HTTP service registers yielded requests before advancing this iterator.
        """
        for region_id, region in regions.items():
            yield from self.build_requests({region_id: region})
            progress.close_region(region_id)

    def acquire(
        self,
        regions: dict[str, Region],
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        progress = AcquisitionProgress(
            regions,
            context.on_region_ready,
            quiet=context.reporting is not None,
        )
        return context.http.download(
            requests=self.plan_requests(regions, progress),
            region_ids=regions.keys(),
            output_dir=context.resolve_acquire_output(options.output_dir, regions),
            skip_existing=options.skip_existing,
            max_retries=options.max_retries,
            max_workers=options.max_workers,
            chunk_size=options.chunk_size,
            retry_delay=options.retry_delay,
            materialize=lambda report: self.materialize(report, context, options),
            reuse_existing=lambda request, output_dir, filenames: self.reuse_existing(
                request,
                output_dir,
                filenames,
                options,
            ),
            progress=progress,
            reporting=context.reporting,
            pipeline_name=context.pipeline_name,
        )
