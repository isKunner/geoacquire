#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: acquisition.py
# @Time    : 2026/8/31
# @Author  : Kevin
# @Describe: Track shared file dependencies and publish complete Regions.

from collections.abc import Callable, Iterable
from dataclasses import replace
from threading import Lock

from .models import AcquisitionReport, AssetStatus, Failure, RegionResult


class AcquisitionProgress:
    """Small, thread-safe ledger between planning, acquisition and Pipeline.

    Planning registers file -> Region dependencies, then closes each Region's
    plan. Workers complete files only AFTER source conversion has succeeded.
    A closed plan with no pending or failed files can be handed to postprocess.
    The callback must only enqueue work: never do raster processing under this lock.

    Completed file results are retained so a later Region can reuse a file that
    has already finished (even when keep_download removed its original LAZ/JPG).

    Example: register('shared.laz', ['0', '10']), close both Regions, then
    complete('shared.laz', one_file_report). Conversion happens once; this
    ledger supplies a LocalAsset alias to each Region and publishes each once.
    """

    def __init__(
        self,
        region_ids: Iterable[str],
        on_region_ready: Callable[[str, RegionResult], None] | None = None,
        quiet: bool = False,
    ):
        self.report = AcquisitionReport.for_regions(region_ids)
        self._on_region_ready = on_region_ready
        self._quiet = quiet
        self._pending = {region_id: set() for region_id in self.report.regions}
        self._targets: dict[str, set[str]] = {}
        self._completed: dict[str, AcquisitionReport] = {}
        self._closed: set[str] = set()
        self._published: set[str] = set()
        self._lock = Lock()

    def register(self, key: str, region_ids: Iterable[str]) -> None:
        """Register dependencies before closing a plan; duplicates are harmless."""
        with self._lock:
            targets = self._targets.setdefault(key, set())
            for region_id in region_ids:
                if region_id in targets:
                    continue
                if region_id in self._closed:
                    raise RuntimeError(f'Cannot add file {key} to closed Region {region_id}')
                targets.add(region_id)
                if key in self._completed:
                    self._deliver(region_id, self._completed[key])
                else:
                    self._pending[region_id].add(key)

    def complete(self, key: str, result: AcquisitionReport) -> None:
        """Publish a physical file once, including conversion failures, to all users."""
        with self._lock:
            if key in self._completed:
                raise RuntimeError(f'File completed twice: {key}')
            self._completed[key] = result
            for region_id in self._targets[key]:
                self._pending[region_id].remove(key)
                self._deliver(region_id, result)
                self._publish_if_ready(region_id)

    def close_region(self, region_id: str) -> None:
        """Promise that this Region will not acquire any more dependencies."""
        with self._lock:
            self._closed.add(region_id)
            self._publish_if_ready(region_id)

    def close_all(self) -> None:
        """Safe fallback after a request stream has been completely consumed."""
        for region_id in self.report.regions:
            self.close_region(region_id)

    def _deliver(self, region_id: str, report: AcquisitionReport) -> None:
        for status, asset in report.iter_assets():
            shared = asset.region_id != region_id
            self.report.add_asset(
                replace(asset, region_id=region_id),
                AssetStatus.REUSED if shared else status,
            )
        for result in report.regions.values():
            for failure in result.failed:
                self.report.add_failure(replace(failure, region_id=region_id))
        if not any(result.asset_count or result.failed for result in report.regions.values()):
            self.report.add_failure(Failure(region_id, 'materialize', 'Source produced no usable assets'))

    def _publish_if_ready(self, region_id: str) -> None:
        if region_id not in self._closed or self._pending[region_id] or region_id in self._published:
            return
        self._published.add(region_id)
        result = self.report.regions[region_id]
        if result.failed or not result.asset_count:
            if not self._quiet:
                print(
                    f'[acquire/region] region={region_id} not_ready '
                    f'failures={len(result.failed)} assets={result.asset_count}'
                )
            return
        if self._on_region_ready is not None:
            # Postprocessors may append products to their report. Give them their
            # own lists so acquisition and postprocessing never mutate one ledger.
            snapshot = RegionResult(
                success=list(result.success),
                skipped=list(result.skipped),
                failed=list(result.failed),
                reused=list(result.reused),
            )
            self._on_region_ready(region_id, snapshot)
