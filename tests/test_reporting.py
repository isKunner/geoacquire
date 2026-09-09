#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: test_reporting.py
# @Time    : 2026/9/2
# @Author  : Kevin
# @Describe: Offline tests for terminal/log/JSON reporting and lightweight resume.

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from geoacquire.core.config import PipelineConfig, ReportingConfig
from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import (
    AcquisitionReport,
    AcquireOptions,
    AssetKind,
    AssetStatus,
    DownloadRequest,
    LocalAsset,
    Region,
)
from geoacquire.core.pipeline import PipelineRunner
from geoacquire.core.run_state import RunStateTracker
from geoacquire.core.source import HTTPSource
from geoacquire.postprocess.base import BasePostprocessor
from geoacquire.services.http_download import HTTPDownloadService, _TransferResult
from geoacquire.services.run_reporting import RunReportingService


class _Source(HTTPSource):
    def build_requests(self, regions):
        for region_id in regions:
            yield DownloadRequest(
                region_id,
                f'asset-{region_id}',
                f'https://example.invalid/{region_id}',
                f'{region_id}.laz',
            )


class _HTTP(HTTPDownloadService):
    def __init__(self, fail: bool = False):
        self.fail = fail

    def _download_one(self, request, output_dir, skip_existing, chunk_size):
        path = os.path.join(output_dir, request.filename)
        if self.fail:
            return _TransferResult(request, 'failed', path, 'simulated download failure')
        return _TransferResult(request, 'success', path)


class _FinalTIF(BasePostprocessor):
    @staticmethod
    def _path(region: Region) -> str:
        assert region.target_path is not None
        return os.path.splitext(region.target_path)[0] + '_final.tif'

    def expected_output(self, region, context, default_output_dir):
        return LocalAsset(region.region_id, region.region_id, self._path(region), AssetKind.RASTER, 'final')

    def process(self, regions, report, context):
        region = next(iter(regions.values()))
        path = self._path(region)
        Path(path).write_bytes(b'final')
        report.add_asset(
            LocalAsset(region.region_id, region.region_id, path, AssetKind.RASTER, 'final'),
            AssetStatus.SUCCESS,
        )
        return report


class ReportingTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        temporary = tempfile.TemporaryDirectory(prefix='.test_reporting_', dir=root)
        self.addCleanup(temporary.cleanup)
        self.path = temporary.name

    def _regions(self, count: int):
        result = {}
        for index in range(count):
            target = os.path.join(self.path, f'{index}.tif')
            Path(target).write_bytes(b'target')
            result[str(index)] = Region(
                str(index),
                (0, 0, 1, 1),
                metadata={'target_path': target},
            )
        return result

    def _run(self, regions, source=None, http=None, overwrite=True):
        source = source or _Source()
        http = http or _HTTP()
        reporting = RunReportingService(
            self.path,
            ReportingConfig('state.json', 'run.log', overwrite),
        )
        pipeline = PipelineConfig(
            source,
            AcquireOptions(output_dir=self.path, max_workers=2, max_retries=0),
            [_FinalTIF()],
            name='reporting',
            postprocess_workers=1,
        )
        provider = type('Provider', (), {'get_regions': lambda _: regions})()
        try:
            result = PipelineRunner(RuntimeContext(self.path, http, reporting=reporting)).run(
                provider, [pipeline],
            )
        finally:
            reporting.close()
        return result

    def test_terminal_log_and_json_have_deliberate_detail_levels(self):
        output = StringIO()
        with redirect_stdout(output):
            self._run(self._regions(10))

        terminal_lines = output.getvalue().splitlines()
        log = Path(self.path, 'run.log').read_text(encoding='utf-8')
        payload = json.loads(Path(self.path, 'state.json').read_text(encoding='utf-8'))

        self.assertEqual(sum('[progress]' in line for line in terminal_lines), 10)
        self.assertNotIn('0.laz', output.getvalue())
        for line in terminal_lines:
            self.assertIn(line, log)
        self.assertEqual(log.count('[tif/success]'), 10)
        self.assertEqual(payload['status'], 'complete')
        pipeline = payload['pipelines']['reporting']
        self.assertEqual(pipeline['summary']['tifs']['complete'], 10)
        self.assertEqual(pipeline['summary']['laz']['total'], 10)
        self.assertEqual(pipeline['laz_files']['0.laz']['target_region_ids'], ['0'])
        self.assertEqual(payload['last_checkpoint']['reason'], 'run_complete')

    def test_errors_are_immediately_available_in_log_and_json(self):
        output = StringIO()
        with redirect_stdout(output):
            self._run(self._regions(1), http=_HTTP(fail=True))

        log = Path(self.path, 'run.log').read_text(encoding='utf-8')
        payload = json.loads(Path(self.path, 'state.json').read_text(encoding='utf-8'))
        pipeline = payload['pipelines']['reporting']
        self.assertIn('[laz/failed]', log)
        self.assertIn('[tif/failed]', log)
        self.assertEqual(pipeline['summary']['laz']['failed'], 1)
        self.assertEqual(pipeline['summary']['tifs']['failed'], 1)
        self.assertEqual(
            {item['category'] for item in payload['errors']},
            {'laz', 'tif'},
        )

    def test_resume_trusts_existing_final_tif_and_retains_old_dependencies(self):
        regions = self._regions(1)
        with redirect_stdout(StringIO()):
            self._run(regions, http=_HTTP(), overwrite=True)
            self._run(regions, http=_HTTP(fail=True), overwrite=False)

        payload = json.loads(Path(self.path, 'state.json').read_text(encoding='utf-8'))
        pipeline = payload['pipelines']['reporting']
        self.assertEqual(pipeline['summary']['tifs']['existing'], 1)
        self.assertEqual(pipeline['summary']['laz']['total'], 0)
        self.assertIn('0.laz', pipeline['historical_laz_files'])
        self.assertEqual(payload['resume_count'], 1)

    def test_interrupt_saves_current_json_immediately(self):
        regions = self._regions(1)
        reporting = RunReportingService(
            self.path,
            ReportingConfig('state.json', 'run.log', True),
        )
        try:
            with redirect_stdout(StringIO()):
                reporting.start_pipeline('reporting', regions, {}, (), 8)
                reporting.fail_run('Interrupted by user', interrupted=True)
        finally:
            reporting.close()

        payload = json.loads(Path(self.path, 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['status'], 'interrupted')
        self.assertEqual(payload['last_checkpoint']['reason'], 'interrupted')
        self.assertEqual(payload['errors'][0]['category'], 'interrupted')

    def test_worker_utilization_is_integrated_from_events_without_polling(self):
        current = [0.0]
        tracker = RunStateTracker(
            monotonic=lambda: current[0],
            wall_clock=lambda: f't={current[0]}',
        )
        region = Region('0', (0, 0, 1, 1))
        tracker.start_pipeline('reporting', {'0': region}, {}, (), 2)
        first = DownloadRequest('0', 'first', 'https://example.invalid/1', '1.laz')
        second = DownloadRequest('0', 'second', 'https://example.invalid/2', '2.laz')
        tracker.record_request('reporting', '1.laz', first)
        tracker.record_request('reporting', '2.laz', second)

        tracker.worker_started('reporting', '1.laz')
        current[0] = 1.0
        tracker.worker_started('reporting', '2.laz')
        current[0] = 3.0
        tracker.file_finished('reporting', '1.laz', AcquisitionReport.for_regions(['0']))
        current[0] = 5.0
        tracker.file_finished('reporting', '2.laz', AcquisitionReport.for_regions(['0']))

        self.assertEqual(tracker.summary('reporting')['workers']['average_active'], 1.4)


if __name__ == '__main__':
    unittest.main()
