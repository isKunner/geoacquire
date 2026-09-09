#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: test_streaming.py
# @Time    : 2026/8/31
# @Author  : Kevin
# @Describe: Deterministic A/B scheduling, shared files and early raster outputs.

import os
import tempfile
import threading
import unittest
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import laspy
import numpy as np
import rasterio
from pyproj import CRS
from rasterio.transform import from_bounds

from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.config import PipelineConfig, load_config
from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import AcquisitionReport, AcquireOptions, DownloadRequest, Failure, LocalAsset, Region
from geoacquire.core.pipeline import PipelineRunner
from geoacquire.core.source import HTTPSource
from geoacquire.postprocess.base import BasePostprocessor
from geoacquire.postprocess.raster_align import RasterAlignToTargetPostprocessor
from geoacquire.services.http_download import HTTPDownloadService, _TransferResult
from geoacquire.sources.copdem.source import CopDEMSource
from geoacquire.sources.usgs.lidar import USGSLidarSource, _LidarTile, _WorkunitPlan
from geoacquire.sources.usgs.lidar_rasterizer import LidarRasterizer, RasterizedGrid
from geoacquire.sources.rs.google import GoogleSource
from PIL import Image


def file_report(region_id='a', path='tile.tif', failed=False, status='success'):
    report = AcquisitionReport.for_regions([region_id])
    if failed:
        report.add_failure(Failure(region_id, path, 'simulated failure'))
    else:
        report.add_asset(LocalAsset(region_id, path, path, 'raster', 'dem'), status)
    return report


class _Source(HTTPSource):
    def __init__(self, files, convert=None, before_region=None):
        self.files = files
        self.convert = convert
        self.before_region = before_region

    def build_requests(self, regions):
        for region_id in regions:
            if self.before_region:
                self.before_region(region_id)
            for filename in self.files[region_id]:
                yield DownloadRequest(region_id, filename, f'https://example.invalid/{filename}', filename)

    def materialize(self, report, context, options):
        return self.convert(report) if self.convert else report


class _HTTP(HTTPDownloadService):
    def __init__(self, transfer=None):
        self.transfer = transfer
        self.calls = []
        self.threads = {}

    def _download_one(self, request, output_dir, skip_existing, chunk_size):
        self.calls.append(request.filename)
        self.threads[request.filename] = threading.get_ident()
        if self.transfer:
            self.transfer(request)
        return _TransferResult(request, 'success', os.path.join(output_dir, request.filename))


class _Processor(BasePostprocessor):
    def __init__(self, process=None):
        self.action = process
        self.calls = []

    def process(self, regions, report, context):
        self.calls.extend(regions)
        if self.action:
            self.action(next(iter(regions)), report)
        return report


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(prefix='.test_stream_', dir=self.root)
        self.addCleanup(self.temporary.cleanup)
        self.path = self.temporary.name

    def run_pipeline(self, source, service, regions, processors, workers=2, post_workers=1):
        context = RuntimeContext(self.path, service)
        pipeline = PipelineConfig(
            source, AcquireOptions(output_dir=self.path, max_workers=workers, max_retries=0),
            processors, name='stream', postprocess_workers=post_workers,
        )
        return PipelineRunner(context).run(SimpleNamespace(get_regions=lambda: regions), [pipeline])['stream']

    @staticmethod
    def regions(*names):
        return {name: Region(name, (0, 0, 1, 1)) for name in names}

    def test_same_worker_converts_and_first_region_finishes_before_last_download(self):
        release = threading.Event()
        last_started = threading.Event()
        first_done = threading.Event()
        threads = {}

        def transfer(request):
            if request.region_id == 'b':
                last_started.set()
                if not release.wait(10):
                    raise TimeoutError('test did not release last download')

        def convert(report):
            asset = next(report.iter_assets())[1]
            threads[asset.asset_id] = threading.get_ident()
            return report

        def process(region_id, report):
            self.assertTrue(threading.current_thread().name.startswith('postprocess'))
            if region_id == 'a':
                first_done.set()

        service = _HTTP(transfer)
        processor = _Processor(process)
        source = _Source({'a': ['first'], 'b': ['last']}, convert)
        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(self.run_pipeline, source, service, self.regions('a', 'b'), [processor])
            try:
                self.assertTrue(last_started.wait(5))
                self.assertTrue(first_done.wait(5))
                self.assertFalse(future.done())
            finally:
                release.set()
            result = future.result(10)
        self.assertEqual(threads, service.threads)
        self.assertEqual(Counter(processor.calls), {'a': 1, 'b': 1})
        self.assertEqual(result.summary()['failed'], 0)

    def test_slow_planning_does_not_hide_a_completed_region(self):
        release = threading.Event()
        planning = threading.Event()
        ready = threading.Event()

        def before_region(region_id):
            if region_id == 'b':
                planning.set()
                if not release.wait(10):
                    raise TimeoutError('planner was not released')

        processor = _Processor(lambda region_id, report: ready.set() if region_id == 'a' else None)
        source = _Source({'a': ['a'], 'b': ['b']}, before_region=before_region)
        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(self.run_pipeline, source, _HTTP(), self.regions('a', 'b'), [processor])
            try:
                self.assertTrue(planning.wait(5))
                self.assertTrue(ready.wait(5))
            finally:
                release.set()
            future.result(10)

    def test_slow_postprocessing_does_not_hold_an_acquisition_worker(self):
        release = threading.Event()
        processing = threading.Event()
        second_converted = threading.Event()

        def process(region_id, report):
            if region_id == 'a':
                processing.set()
                if not release.wait(10):
                    raise TimeoutError('postprocessor was not released')

        def convert(report):
            if 'b' in report.regions:
                second_converted.set()
            return report

        source = _Source({'a': ['a'], 'b': ['b']}, convert)
        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(self.run_pipeline, source, _HTTP(), self.regions('a', 'b'), [_Processor(process)], 1)
            try:
                self.assertTrue(processing.wait(5))
                self.assertTrue(second_converted.wait(5))
            finally:
                release.set()
            future.result(10)

    def test_late_shared_file_alias_reuses_materialized_output_once(self):
        converted = threading.Event()
        calls = []

        def convert(report):
            calls.append('convert')
            output = file_report('a', os.path.join(self.path, 'shared_dsm.tif'))
            converted.set()
            return output

        def before_region(region_id):
            if region_id == 'b' and not converted.wait(5):
                raise TimeoutError('first conversion did not finish')

        source = _Source({'a': ['shared.laz'], 'b': ['shared.laz']}, convert, before_region)
        service = _HTTP()
        processor = _Processor()
        report = self.run_pipeline(source, service, self.regions('a', 'b'), [processor], workers=1)
        self.assertEqual(service.calls, ['shared.laz'])
        self.assertEqual(calls, ['convert'])
        self.assertEqual(report.regions['b'].reused[0].path, report.regions['a'].success[0].path)
        self.assertEqual(Counter(processor.calls), {'a': 1, 'b': 1})

    def test_closed_plan_and_all_files_are_required_even_with_concurrent_completions(self):
        ready = []
        progress = AcquisitionProgress(['a'], lambda region_id, result: ready.append(region_id))
        progress.register('1', ['a'])
        progress.complete('1', file_report(path='1'))
        self.assertEqual(ready, [])
        progress.register('2', ['a'])
        progress.register('3', ['a'])
        progress.close_region('a')
        barrier = threading.Barrier(2)

        def complete(key):
            barrier.wait(5)
            progress.complete(key, file_report(path=key))

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(complete, ['2', '3']))
        progress.close_all()
        self.assertEqual(ready, ['a'])

    def test_shared_failure_blocks_dependents_but_not_other_regions(self):
        def convert(report):
            if 'a' in report.regions:
                raise RuntimeError('bad LAZ')
            return report

        source = _Source({'a': ['bad'], 'b': ['bad'], 'c': ['good']}, convert)
        service = _HTTP()
        processor = _Processor()
        report = self.run_pipeline(source, service, self.regions('a', 'b', 'c'), [processor])
        self.assertEqual(processor.calls, ['c'])
        self.assertEqual(Counter(service.calls), {'bad': 1, 'good': 1})
        self.assertEqual(report.summary()['failed'], 2)

    def test_transfer_failure_does_not_call_conversion(self):
        service = _HTTP(lambda request: (_ for _ in ()).throw(RuntimeError('download failed')))
        convert = Mock()
        processor = _Processor()
        report = self.run_pipeline(_Source({'a': ['bad']}, convert), service, self.regions('a'), [processor])
        convert.assert_not_called()
        self.assertEqual(processor.calls, [])
        self.assertEqual(report.summary()['failed'], 1)

    def test_two_postprocess_workers_keep_each_region_chain_ordered(self):
        barrier = threading.Barrier(2)
        order = {'a': [], 'b': []}

        def first(region_id, report):
            order[region_id].append('first')
            barrier.wait(5)

        def second(region_id, report):
            order[region_id].append('second')

        report = self.run_pipeline(
            _Source({'a': ['a'], 'b': ['b']}), _HTTP(), self.regions('a', 'b'),
            [_Processor(first), _Processor(second)], post_workers=2,
        )
        self.assertEqual(report.summary()['failed'], 0)
        self.assertEqual(order, {'a': ['first', 'second'], 'b': ['first', 'second']})

    def test_failed_postprocessor_stops_only_its_region_chain(self):
        def first(region_id, report):
            if region_id == 'a':
                raise RuntimeError('mosaic failed')

        second = _Processor()
        report = self.run_pipeline(
            _Source({'a': ['a'], 'b': ['b']}), _HTTP(), self.regions('a', 'b'),
            [_Processor(first), second], post_workers=2,
        )
        self.assertEqual(second.calls, ['b'])
        self.assertEqual(report.summary()['failed'], 1)

    def test_copdem_keeps_download_extract_together_and_publishes_regions_early(self):
        source = CopDEMSource('test@example.invalid', 'not-a-real-password')
        source.client.authenticate = Mock()
        ready = threading.Event()
        calls = []

        def download_tile(tile_name, **kwargs):
            calls.append(tile_name)
            if tile_name == 'other' and not ready.wait(5):
                raise RuntimeError('first Region did not postprocess early')
            return 'success', os.path.join(self.path, tile_name + '.tif'), tile_name

        source.client.download_tile = download_tile
        processor = _Processor(lambda region_id, report: ready.set() if region_id == 'a' else None)
        with patch('geoacquire.sources.copdem.source.plan_region_tiles', side_effect=[['shared'], ['shared', 'other']]):
            report = self.run_pipeline(source, _HTTP(), self.regions('a', 'b'), [processor])
        self.assertEqual(calls, ['shared', 'other'])
        self.assertEqual(report.summary()['failed'], 0)
        self.assertEqual(Counter(processor.calls), {'a': 1, 'b': 1})

    def test_real_lidar_materialization_and_alignment_produce_early_outputs(self):
        """Small genuine LAZ -> DSM -> aligned TIF smoke, with three target Regions."""
        crs = CRS.from_epsg(32616)
        regions = {}
        for region_id, left in [('a', 500001), ('b', 500003), ('c', 500021)]:
            bounds = (left, 4100001, left + 4, 4100005)
            target = os.path.join(self.path, region_id + '.tif')
            transform = from_bounds(*bounds, 4, 4)
            with rasterio.open(target, 'w', driver='GTiff', height=4, width=4, count=1,
                               dtype='float32', crs=crs, transform=transform) as dataset:
                dataset.write(np.ones((4, 4), dtype='float32'), 1)
            regions[region_id] = Region(region_id, bounds, crs.to_string(), {
                'target_path': target, 'shape': (4, 4), 'transform': tuple(transform),
                'resolution': (1, 1), 'dtype': 'float32', 'nodata': None,
            })

        plans = []
        for filename, left, name in [('shared.laz', 500000, 'first'), ('slow.laz', 500020, 'last')]:
            header = laspy.LasHeader(point_format=3, version='1.2')
            header.add_crs(crs)
            header.offsets = np.array([left, 4100000, 0])
            las = laspy.LasData(header)
            x, y = np.meshgrid(np.arange(8) + left, np.arange(8) + 4100000)
            las.x, las.y = x.ravel(), y.ravel()
            las.z = np.full(64, 100.0)
            las.classification = np.full(64, 2, dtype='uint8')
            las.write(os.path.join(self.path, filename))
            workunit = {'workunit': name, 'lpc_link': name, 'collect_end': '2025' if name == 'first' else '2020'}
            tile = _LidarTile('https://example.invalid/' + filename, filename,
                              (left, 4100000, left + 8, 4100008), crs.to_string(), 'test', 'test')
            plans.append(_WorkunitPlan(workunit, 'test', 1, (tile,), ()))

        source = USGSLidarSource(output_products=['dsm'], keep_download=False,
                                audit_report_path=os.path.join(self.path, 'audit.json'), point_chunk_size=10)
        source.wesm.query_many = Mock(return_value={'a': [plans[0].workunit], 'b': [plans[0].workunit], 'c': [plans[1].workunit]})
        source._load_workunit_plan = lambda workunit: next(plan for plan in plans if plan.workunit == workunit)
        source.rasterizer.rasterize = Mock(wraps=source.rasterizer.rasterize)
        release = threading.Event()
        early_output = threading.Event()

        def transfer(request):
            if request.filename == 'slow.laz' and not release.wait(10):
                raise TimeoutError('last LAZ not released')

        align = RasterAlignToTargetPostprocessor('lidar', input_products=['dsm'], window_size=2)
        original_process = align.process

        def process(ready_regions, report, context):
            result = original_process(ready_regions, report, context)
            if 'a' in ready_regions:
                early_output.set()
            return result

        align.process = process
        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(self.run_pipeline, source, _HTTP(transfer), regions, [align], 2, 2)
            try:
                self.assertTrue(early_output.wait(8))
                self.assertTrue(os.path.isfile(os.path.join(self.path, 'a_lidar.tif')))
                self.assertFalse(future.done())
            finally:
                release.set()
            report = future.result(10)
        self.assertEqual(report.summary()['failed'], 0)
        self.assertEqual(source.rasterizer.rasterize.call_count, 2)
        self.assertEqual(source.wesm.query_many.call_count, 1)
        self.assertFalse(os.path.exists(os.path.join(self.path, 'shared.laz')))
        for region_id, region in regions.items():
            with rasterio.open(os.path.join(self.path, region_id + '_lidar.tif')) as actual:
                with rasterio.open(region.metadata['target_path']) as target:
                    self.assertEqual(actual.transform, target.transform)
                    self.assertEqual(actual.shape, target.shape)
                    self.assertEqual(actual.crs, target.crs)
                self.assertGreater(actual.read(1, masked=True).count(), 0)

    def test_postprocess_worker_option_is_configurable_in_both_defaults(self):
        for filename in ['default.yaml', 'default_zh.yaml']:
            config = load_config([str(self.root / 'configs' / filename)], ['pipelines.usgs_lidar.postprocess_workers=2'])
            self.assertEqual(config.pipelines[0].postprocess_workers, 2)
        with self.assertRaises(ValueError):
            PipelineConfig(_Source({}), AcquireOptions(), postprocess_workers=0)

    def test_lidar_region_closes_before_waiting_for_an_older_workunit(self):
        ready = threading.Event()
        regions = {
            'a': Region('a', (500001, 4100001, 500004, 4100004), 'EPSG:32616',
                        {'target_path': os.path.join(self.path, 'a.tif')}),
            'b': Region('b', (500021, 4100001, 500024, 4100004), 'EPSG:32616',
                        {'target_path': os.path.join(self.path, 'b.tif')}),
        }
        newer = {'workunit': 'newer', 'lpc_link': 'newer', 'collect_end': '2025'}
        older = {'workunit': 'older', 'lpc_link': 'older', 'collect_end': '2020'}
        source = USGSLidarSource(audit_report_path=os.path.join(self.path, 'audit.json'))
        source.wesm.query_many = Mock(return_value={'a': [newer, older], 'b': [older]})

        def plan(workunit):
            if workunit is older and not ready.wait(5):
                raise TimeoutError('Region a must close before older workunit is loaded')
            left = 500000 if workunit is newer else 500020
            tile = _LidarTile('https://example.invalid/' + workunit['workunit'], workunit['workunit'] + '.laz',
                              (left, 4100000, left + 8, 4100008), 'EPSG:32616', 'test', 'test')
            return _WorkunitPlan(workunit, 'test', 1, (tile,), ())

        source._load_workunit_plan = plan
        processor = _Processor(lambda region_id, report: ready.set() if region_id == 'a' else None)
        report = self.run_pipeline(source, _HTTP(), regions, [processor])
        self.assertEqual(report.summary()['failed'], 0)
        self.assertEqual(Counter(processor.calls), {'a': 1, 'b': 1})

    def test_existing_raw_file_is_converted_and_processed_without_http(self):
        raw = os.path.join(self.path, 'existing.bin')
        Path(raw).write_bytes(b'existing')
        converted = []

        def convert(report):
            self.assertEqual(report.summary()['skipped'], 1)
            converted.append('called')
            return report

        processor = _Processor()
        with patch('geoacquire.services.http_download.requests.get', side_effect=AssertionError('unexpected HTTP')):
            report = self.run_pipeline(_Source({'a': ['existing.bin']}, convert), HTTPDownloadService(),
                                       self.regions('a'), [processor])
        self.assertEqual(converted, ['called'])
        self.assertEqual(processor.calls, ['a'])
        self.assertEqual(report.summary()['failed'], 0)

    def test_final_dsm_and_dtm_outputs_bypass_source_acquisition(self):
        for suffix in ('surface', 'terrain'):
            Path(self.path, f'a_{suffix}.tif').write_bytes(b'complete')
        regions = {
            'a': Region(
                'a',
                (0, 0, 1, 1),
                metadata={'target_path': os.path.join(self.path, 'a.tif')},
            ),
        }
        service = _HTTP()
        report = self.run_pipeline(
            _Source({}),
            service,
            regions,
            [
                RasterAlignToTargetPostprocessor('surface', input_products=['dsm']),
                RasterAlignToTargetPostprocessor('terrain', input_products=['dtm']),
            ],
        )

        self.assertEqual(service.calls, [])
        self.assertEqual(report.summary()['skipped'], 2)
        self.assertEqual(
            {asset.product for asset in report.regions['a'].skipped},
            {'dsm', 'dtm'},
        )

    def test_mixed_final_preflight_merges_completed_and_processed_regions(self):
        class ExpectedProcessor(_Processor):
            def expected_output(self, region, context, default_output_dir):
                path = os.path.join(default_output_dir, f'{region.region_id}_final.tif')
                return LocalAsset(region.region_id, region.region_id, path, 'raster', 'dem')

        Path(self.path, 'a_final.tif').write_bytes(b'complete')
        service = _HTTP()
        processor = ExpectedProcessor()

        report = self.run_pipeline(
            _Source({'b': ['b.tif']}),
            service,
            self.regions('a', 'b'),
            [processor],
        )

        self.assertEqual(service.calls, ['b.tif'])
        self.assertEqual(processor.calls, ['b'])
        self.assertEqual(list(report.regions), ['a', 'b'])
        self.assertEqual(report.regions['a'].skipped[0].path, os.path.join(self.path, 'a_final.tif'))
        self.assertEqual(report.regions['b'].success[0].path, os.path.join(self.path, 'b.tif'))

    def test_lidar_derived_preflight_respects_keep_download(self):
        request = DownloadRequest(
            'a', 'tile', 'https://example.invalid/tile.laz', 'tile.laz',
            'point_cloud', 'laz',
        )
        Path(self.path, 'tile_dsm.tif').write_bytes(b'complete')
        names = HTTPDownloadService._scan_filenames(self.path)
        options = AcquireOptions(output_dir=self.path)

        disposable = USGSLidarSource(output_products=['dsm'], keep_download=False)
        reused = disposable.reuse_existing(request, self.path, names, options)
        self.assertIsNotNone(reused)
        self.assertEqual(reused.summary()['skipped'], 1)

        retained = USGSLidarSource(output_products=['dsm'], keep_download=True)
        self.assertIsNone(retained.reuse_existing(request, self.path, names, options))
        Path(self.path, 'tile.laz').write_bytes(b'raw')
        names = HTTPDownloadService._scan_filenames(self.path)
        self.assertIsNotNone(retained.reuse_existing(request, self.path, names, options))

    def test_xyz_converts_real_jpeg_before_publishing_a_region(self):
        source = GoogleSource(zoom=2, keep_download=False)
        regions = {'a': Region('a', (1, 1, 2, 2))}
        requests = list(source.build_requests(regions))
        self.assertEqual(len(requests), 1)
        raw = os.path.join(self.path, requests[0].filename)
        Image.new('RGB', (8, 8), color=(20, 40, 60)).save(raw)

        def process(region_id, report):
            asset = next(report.iter_assets())[1]
            self.assertEqual(asset.kind, 'raster')
            self.assertFalse(os.path.exists(raw))
            with rasterio.open(asset.path) as dataset:
                self.assertEqual(dataset.crs.to_epsg(), 3857)
                self.assertEqual(dataset.count, 3)
                self.assertEqual(dataset.shape, (8, 8))

        report = self.run_pipeline(source, _HTTP(), regions, [_Processor(process)])
        self.assertEqual(report.summary()['failed'], 0)

    def test_atomic_dsm_failure_does_not_replace_an_existing_raster(self):
        path = os.path.join(self.path, 'dsm.tif')
        transform = from_bounds(0, 0, 2, 2, 2, 2)
        original = RasterizedGrid(np.ones((2, 2), dtype='float32'), CRS.from_epsg(4326).to_wkt(), transform)
        LidarRasterizer.save(path, original)
        before = Path(path).read_bytes()
        with patch('geoacquire.sources.usgs.lidar_rasterizer.os.replace', side_effect=OSError('simulated commit failure')):
            with self.assertRaises(OSError):
                LidarRasterizer.save(path, original)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertFalse(os.path.isfile(path + '.part'))

    def test_progress_reuses_completed_file_for_a_late_region(self):
        ready = []
        progress = AcquisitionProgress(['a', 'b'], lambda region_id, result: ready.append(region_id))
        progress.register('shared.laz', ['a'])
        progress.close_region('a')
        progress.complete('shared.laz', file_report('a', 'shared_dsm.tif'))
        progress.register('shared.laz', ['b'])
        progress.close_region('b')
        self.assertEqual(ready, ['a', 'b'])
        self.assertEqual(progress.report.regions['b'].reused[0].path, 'shared_dsm.tif')


if __name__ == '__main__':
    unittest.main()
