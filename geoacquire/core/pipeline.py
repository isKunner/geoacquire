#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: pipeline.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Stable orchestration from RegionProvider through Source acquisition and optional post-processing.

import os
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from threading import Lock
from typing import Any

from .context import RuntimeContext
from .models import AcquisitionReport, Failure, LocalAsset, Region, RegionResult


def format_region_ids(region_ids: Iterable[str], limit: int = 20) -> str:
    """Keep large directory runs readable while retaining a deterministic sample."""
    names = list(region_ids)
    if len(names) <= limit:
        return f'count={len(names)} ids={names}'
    head = names[:limit - 3]
    tail = names[-3:]
    return f'count={len(names)} ids={head + ["..."] + tail}'


class PipelineRunner:
    """Run configured objects without knowing any concrete Source or post-processor class."""

    def __init__(self, context: RuntimeContext):
        self.context = context

    def run(self, region_provider: Any, pipelines: list[Any]) -> dict[str, AcquisitionReport]:
        regions = region_provider.get_regions()
        reports: dict[str, AcquisitionReport] = {}
        if self.context.reporting is None:
            print(f'[run] regions: {format_region_ids(regions)}')

        for index, pipeline in enumerate(pipelines):
            label = pipeline.name or f'pipeline_{index}_{type(pipeline.source).__name__}'
            if not pipeline.enable:
                if self.context.reporting is None:
                    print(f'[run] {label}: disabled')
                continue

            if self.context.reporting is None:
                print(f'[run] {label}: acquiring with {type(pipeline.source).__name__}')
            report = self._run_pipeline(regions, pipeline, label)

            summary = report.summary()
            if self.context.reporting is None:
                print(
                    f'[run] {label}: asset_success={summary["success"]} '
                    f'asset_skipped={summary["skipped"]} asset_reused={summary["reused"]} '
                    f'failed={summary["failed"]}'
                )
            reports[label] = report

        if self.context.reporting is not None:
            self.context.reporting.complete_run()
        return reports

    def _run_pipeline(self, regions: dict[str, Region], pipeline: Any, label: str) -> AcquisitionReport:
        if not pipeline.postprocess:
            context = replace(self.context, pipeline_name=label)
            if context.reporting is not None:
                context.reporting.start_pipeline(
                    label, regions, {}, (), pipeline.acquire.max_workers,
                )
            report = pipeline.source.acquire(regions, context, pipeline.acquire)
            if context.reporting is not None:
                for region_id, result in report.regions.items():
                    if not result.asset_count and not result.failed:
                        continue
                    snapshot = AcquisitionReport({region_id: result})
                    context.reporting.tif_ready(label, region_id)
                    context.reporting.tif_started(label, region_id)
                    context.reporting.tif_finished(label, region_id, snapshot)
                context.reporting.finish_pipeline(label)
            return report

        completed, pending_regions, expected = self._reuse_final_outputs(regions, pipeline, label)
        reporting = self.context.reporting
        if reporting is not None:
            reporting.start_pipeline(
                label,
                regions,
                {
                    region_id: [asset.path for asset in assets]
                    for region_id, assets in expected.items()
                },
                completed,
                pipeline.acquire.max_workers,
            )
        if not pending_regions:
            report = AcquisitionReport(completed)
            if reporting is not None:
                reporting.finish_pipeline(label)
            return report

        # Only B lives here. Source owns A (download + conversion). The callback
        # just submits a closed Region snapshot; it never blocks A doing a mosaic.
        futures: dict[str, Future] = {}
        submit_lock = Lock()
        with ThreadPoolExecutor(
            max_workers=pipeline.postprocess_workers,
            thread_name_prefix='postprocess',
        ) as executor:
            def enqueue(region_id: str, result: RegionResult) -> None:
                with submit_lock:
                    if region_id in futures:
                        return
                    if reporting is not None:
                        reporting.tif_ready(label, region_id)
                    else:
                        print(f'[run/ready] {label} region={region_id} assets={result.asset_count}; queued postprocess')
                    snapshot = AcquisitionReport({region_id: result})
                    futures[region_id] = executor.submit(
                        self._process_region, regions[region_id], snapshot, pipeline, label, context,
                    )

            context = replace(self.context, on_region_ready=enqueue, pipeline_name=label)
            report = pipeline.source.acquire(pending_regions, context, pipeline.acquire)
            # Custom BaseSources may still return only a final report. They remain
            # compatible, but must publish readiness to obtain early postprocess.
            for region_id, result in report.regions.items():
                if region_id not in futures and result.asset_count and not result.failed:
                    enqueue(
                        region_id,
                        RegionResult(
                            success=list(result.success),
                            skipped=list(result.skipped),
                            failed=[],
                            reused=list(result.reused),
                        ),
                    )

        # All B jobs have finished. Do not run the processor chain a second time.
        for region_id, future in futures.items():
            report.regions[region_id] = future.result().regions[region_id]
        report.regions = {
            region_id: (
                completed[region_id]
                if region_id in completed
                else report.regions[region_id]
            )
            for region_id in regions
        }
        if reporting is not None:
            reporting.finish_pipeline(label)
        return report

    def _reuse_final_outputs(
        self,
        regions: dict[str, Region],
        pipeline: Any,
        label: str,
    ) -> tuple[
        dict[str, RegionResult],
        dict[str, Region],
        dict[str, list[LocalAsset]],
    ]:
        """Batch-check inferable final outputs before any Source planning/download."""
        if not pipeline.acquire.skip_existing:
            expected = self._expected_final_outputs(regions, pipeline)
            return {}, regions, expected
        try:
            default_output_dir = self.context.resolve_acquire_output(pipeline.acquire.output_dir, regions)
        except ValueError:
            # An explicit processor output can still be reused without resolving
            # a common acquisition directory. Pending Regions will surface the
            # original configuration error when Source acquisition begins.
            default_output_dir = None
        expected = self._expected_final_outputs(regions, pipeline, default_output_dir)

        directory_names: dict[str, set[str]] = {}
        for assets in expected.values():
            for asset in assets:
                directory = os.path.dirname(os.path.abspath(asset.path))
                if directory in directory_names:
                    continue
                if self.context.reporting is not None:
                    directory_names[directory] = set(
                        self.context.reporting.scan_filenames(directory)
                    )
                else:
                    names: set[str] = set()
                    try:
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                try:
                                    if entry.is_file():
                                        names.add(os.path.normcase(entry.name))
                                except OSError:
                                    continue
                    except OSError:
                        pass
                    directory_names[directory] = names

        completed: dict[str, RegionResult] = {}
        for region_id, assets in expected.items():
            if all(
                os.path.normcase(os.path.basename(asset.path))
                in directory_names.get(os.path.dirname(os.path.abspath(asset.path)), set())
                for asset in assets
            ):
                completed[region_id] = RegionResult(skipped=assets)
        if completed and self.context.reporting is None:
            print(
                f'[run/preflight] {label} completed_regions={len(completed)} '
                f'final_assets={sum(result.asset_count for result in completed.values())} '
                f'action=reuse_before_acquire'
            )
        pending = {
            region_id: region
            for region_id, region in regions.items()
            if region_id not in completed
        }
        return completed, pending, expected

    def _expected_final_outputs(
        self,
        regions: dict[str, Region],
        pipeline: Any,
        default_output_dir: str | None = None,
    ) -> dict[str, list[LocalAsset]]:
        """Infer terminal files once for preflight, state tracking, and logging."""
        if default_output_dir is None:
            try:
                default_output_dir = self.context.resolve_acquire_output(
                    pipeline.acquire.output_dir, regions,
                )
            except ValueError:
                default_output_dir = None
        expected: dict[str, list[LocalAsset]] = {}
        for region_id, region in regions.items():
            terminal_products: set[str] = set()
            for processor in reversed(pipeline.postprocess):
                asset = processor.expected_output(region, self.context, default_output_dir)
                if asset is None or asset.product in terminal_products:
                    continue
                terminal_products.add(asset.product)
                expected.setdefault(region_id, []).append(asset)
        return expected

    def _process_region(
        self,
        region: Region,
        report: AcquisitionReport,
        pipeline: Any,
        label: str,
        context: RuntimeContext,
    ) -> AcquisitionReport:
        """One B task owns the entire ordered processor chain of one Region."""
        region_id = region.region_id
        if context.reporting is not None:
            context.reporting.tif_started(label, region_id)
        for processor in pipeline.postprocess:
            name = type(processor).__name__
            if context.reporting is None:
                print(f'[run/postprocess] {label} region={region_id} start={name}')
            try:
                report = processor.process({region_id: region}, report, context)
            except Exception as exc:
                report.add_failure(Failure(region_id, name, str(exc)))
                if context.reporting is None:
                    print(f'[run/postprocess] {label} region={region_id} error={exc}')
            if report.regions[region_id].failed:
                if context.reporting is None:
                    print(f'[run/postprocess] {label} region={region_id} failed={name}')
                break
        else:
            if context.reporting is None:
                print(f'[run/postprocess] {label} region={region_id} finished')
        if context.reporting is not None:
            context.reporting.tif_finished(label, region_id, report)
        return report
