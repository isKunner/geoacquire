#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: test_boundaries.py
# @Time    : 2026/8/31
# @Author  : Kevin
# @Describe: Offline regressions for transfer integrity, cache isolation and raster grids.

import os
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock, patch

import laspy
import numpy as np
import rasterio
import requests
from pyproj import CRS
from rasterio.transform import from_origin

from geoacquire.core.config import deep_merge
from geoacquire.core.context import RuntimeContext
from geoacquire.core.geo import horizontal_crs
from geoacquire.core.models import (
    AcquisitionReport,
    AcquireOptions,
    AssetStatus,
    DownloadRequest,
    LocalAsset,
    Region,
)
from geoacquire.services.http_download import HTTPDownloadService
from geoacquire.sources.copdem.source import CopDEMSource
from geoacquire.sources.usgs.lidar import USGSLidarSource
from geoacquire.sources.usgs.lidar_rasterizer import LidarRasterizer
from geoacquire.sources.usgs.wesm import WESMClient
from scripts.sample_lidar_headers import sample_header


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, 'configs', 'usgs_lidar_projects.yaml')


def response(status, payload=b'', **headers):
    """A real requests.Response with an in-memory body; never opens a socket."""
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers)
    result._content = payload
    result._content_consumed = True
    return result


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.test_boundary_', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.request = DownloadRequest('0', 'tile', 'https://example.invalid/tile', 'tile.bin')
        self.destination = os.path.join(self.root, 'tile.bin')
        self.part = self.destination + '.part'

    def write(self, path, payload):
        with open(path, 'wb') as file:
            file.write(payload)

    def read(self, path):
        with open(path, 'rb') as file:
            return file.read()

    def transfer(self, reply):
        with patch('geoacquire.services.http_download.requests.get', return_value=reply) as get:
            result = HTTPDownloadService._download_one(self.request, self.root, True, 2)
            self.assertEqual(get.call_args.kwargs['headers']['Accept-Encoding'], 'identity')
            return result

    def test_http_resumes_at_the_exact_byte_offset(self):
        self.write(self.part, b'abc')
        result = self.transfer(response(206, b'def', **{'Content-Range': 'bytes 3-5/6'}))
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.read(self.destination), b'abcdef')
        self.assertFalse(os.path.exists(self.part))

    def test_http_post_download_sends_form_body(self):
        request = DownloadRequest(
            '0',
            'post-tile',
            'https://example.invalid/download',
            'post.bin',
            method='post',
            data={'file': '123'},
        )
        with patch(
            'geoacquire.services.http_download.requests.post',
            return_value=response(200, b'payload', **{'Content-Length': '7'}),
        ) as post:
            result = HTTPDownloadService._download_one(request, self.root, True, 2)

        self.assertEqual(result.status, 'success')
        self.assertEqual(self.read(os.path.join(self.root, 'post.bin')), b'payload')
        self.assertEqual(post.call_args.kwargs['data'], {'file': '123'})
        self.assertEqual(request.method, 'POST')

    def test_http_ignored_range_rewrites_instead_of_appending(self):
        self.write(self.part, b'abc')
        result = self.transfer(response(200, b'abcdef', **{'Content-Length': '6'}))
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.read(self.destination), b'abcdef')

    def test_http_wrong_range_does_not_corrupt_the_partial_file(self):
        for content_range in ('bytes 0-2/6', 'bytes 3-9/6', 'invalid'):
            with self.subTest(content_range=content_range):
                self.write(self.part, b'abc')
                result = self.transfer(response(206, b'def', **{'Content-Range': content_range}))
                self.assertEqual(result.status, 'failed')
                self.assertEqual(self.read(self.part), b'abc')
                self.assertFalse(os.path.exists(self.destination))

    def test_http_short_body_is_not_published(self):
        result = self.transfer(response(200, b'abc', **{'Content-Length': '6'}))
        self.assertEqual(result.status, 'failed')
        self.assertEqual(self.read(self.part), b'abc')
        self.assertFalse(os.path.exists(self.destination))

    def test_http_complete_partial_can_finish_after_416(self):
        self.write(self.part, b'abcdef')
        result = self.transfer(response(416, **{'Content-Range': 'bytes */6'}))
        self.assertEqual(result.status, 'success')
        self.assertEqual(self.read(self.destination), b'abcdef')

    def test_http_unexpected_status_or_encoding_cannot_publish_empty_files(self):
        for reply in (response(204), response(200, b'abc', **{'Content-Encoding': 'gzip'})):
            with self.subTest(status=reply.status_code):
                result = self.transfer(reply)
                self.assertEqual(result.status, 'failed')
                self.assertFalse(os.path.exists(self.destination))

    def test_copdem_shares_resume_and_complete_partial_handling(self):
        source = CopDEMSource('offline-user', 'offline-password')
        source.client.ensure_token = Mock()
        for reply in (
            response(206, b'def', **{'Content-Range': 'bytes 3-5/6'}),
            response(416, **{'Content-Range': 'bytes */3'}),
        ):
            with self.subTest(status=reply.status_code):
                self.write(self.part, b'abc')
                source.client.session.get = Mock(return_value=reply)
                source.client.download_product('test', self.destination, chunk_size=2)
                expected = b'abcdef' if reply.status_code == 206 else b'abc'
                self.assertEqual(self.read(self.destination), expected)

    def test_copdem_empty_or_local_only_acquisition_never_authenticates(self):
        source = CopDEMSource('offline-user', 'offline-password')
        source.client.authenticate = Mock(side_effect=AssertionError('Unexpected login'))
        context = RuntimeContext(ROOT, HTTPDownloadService())
        options = AcquireOptions(output_dir=self.root, max_retries=0)
        self.assertEqual(source.acquire({}, context, options).summary()['success'], 0)
        tile = 'Copernicus_DSM_COG_10_N33_00_W086_00_DEM'
        # Patch only planning: real download_tile must find the local raster
        # before search/authentication. This file is never opened as a raster.
        self.write(os.path.join(self.root, tile + '.tif'), b'local')
        with patch('geoacquire.sources.copdem.source.plan_region_tiles', return_value=[tile]):
            report = source.acquire({'0': Region('0', (-86, 33, -85, 34))}, context, options)
        self.assertEqual(report.summary()['skipped'], 1)
        source.client.authenticate.assert_not_called()

    def test_wesm_incomplete_body_stays_partial_and_can_resume(self):
        client = WESMClient(self.root)
        replies = [
            response(200, b'abc', **{'Content-Length': '6'}),
            response(206, b'def', **{'Content-Range': 'bytes 3-5/6'}),
        ]
        with patch('geoacquire.sources.usgs.wesm.WESM_MIN_SIZE', 1), patch(
            'geoacquire.sources.usgs.wesm.requests.get', side_effect=replies,
        ):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete transfer'):
                client._ensure_downloaded()
            self.assertFalse(os.path.exists(client.gpkg_path))
            client._ensure_downloaded()
        self.assertEqual(self.read(client.gpkg_path), b'abcdef')

    def test_wesm_legacy_partial_file_is_preserved_for_resume(self):
        client = WESMClient(self.root)
        self.write(client.gpkg_path, b'abc')
        with patch('geoacquire.sources.usgs.wesm.WESM_MIN_SIZE', 6), patch(
            'geoacquire.sources.usgs.wesm.requests.get',
            return_value=response(206, b'def', **{'Content-Range': 'bytes 3-5/6'}),
        ):
            client._ensure_downloaded()
        self.assertEqual(self.read(client.gpkg_path), b'abcdef')

    def test_concurrent_link_cache_writers_have_independent_temporary_files(self):
        sources = [USGSLidarSource(project_catalog_path=CATALOG, link_cache_dir=self.root) for _ in range(2)]
        barrier = threading.Barrier(2)
        replace = os.replace
        temporary_paths = []

        def synchronize_replace(source, destination):
            temporary_paths.append(source)
            barrier.wait(timeout=10)
            replace(source, destination)

        with patch('geoacquire.sources.usgs.base.requests.get', side_effect=lambda *a, **k: response(200, b'links')), patch(
            'geoacquire.sources.usgs.base.os.replace', side_effect=synchronize_replace,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda source: source._fetch_text('https://example.invalid/unit/list.txt'), sources))
        self.assertEqual(results, ['links', 'links'])
        self.assertEqual(len(set(temporary_paths)), 2)
        self.assertFalse(any(name.endswith('.tmp') for name in os.listdir(self.root)))

    def test_workunit_cache_does_not_reuse_another_crs(self):
        source = USGSLidarSource(project_catalog_path=CATALOG)
        source._fetch_text = Mock(return_value='https://example.invalid/tile_4485004383000.laz')
        workunit = {
            'workunit': 'DE_Statewide_1_B23', 'project': 'DE_Statewide_B23',
            'horiz_crs': '32618', 'lpc_link': 'https://example.invalid/de',
        }
        rule = source.catalog.find(workunit)
        source.catalog.find = Mock(return_value=replace(
            rule, schemes=tuple(replace(scheme, crs='workunit') for scheme in rule.schemes),
        ))
        first = source._load_workunit_plan(workunit)
        second = source._load_workunit_plan({**workunit, 'horiz_crs': '26918'})
        self.assertEqual(first.tiles[0].source_crs, 'EPSG:32618')
        self.assertEqual(second.tiles[0].source_crs, 'EPSG:26918')
        self.assertIs(source._load_workunit_plan(workunit), first)

    def test_class_switch_replaces_old_constructor_args_without_mutating_defaults(self):
        base = {'class_path': 'old.Class', 'init_args': {'old_only': 1, 'shared': 2}}
        same = deep_merge(base, {'init_args': {'shared': 3}})
        changed = deep_merge(base, {'class_path': 'new.Class', 'init_args': {'new_only': 4}})
        self.assertEqual(same['init_args'], {'old_only': 1, 'shared': 3})
        self.assertEqual(changed['init_args'], {'new_only': 4})
        self.assertEqual(base['init_args'], {'old_only': 1, 'shared': 2})

    def test_horizontal_crs_example_and_vertical_rejection(self):
        self.assertEqual(horizontal_crs('EPSG:9518').to_epsg(), 4326)
        with self.assertRaises(ValueError):
            horizontal_crs('EPSG:3855')

    def test_header_sampler_keeps_bounds_without_reading_remote_evlrs(self):
        path = self.points([10, 11], [20, 21], [3, 4], [2, 2])
        reply = response(206)
        reply.raw = io.BytesIO(self.read(path))
        with patch('scripts.sample_lidar_headers.requests.get', return_value=reply), patch(
            'scripts.sample_lidar_headers.laspy.open', wraps=laspy.open,
        ) as open_las:
            result = sample_header({'url': 'https://example.invalid/tile.laz'})
        self.assertNotIn('error', result)
        self.assertEqual(result['bounds'], [10, 20, 11, 21])
        self.assertFalse(open_las.call_args.kwargs['read_evlrs'])

    def points(self, x, y, z, classes, crs='EPSG:32618'):
        path = os.path.join(self.root, 'points.las')
        header = laspy.LasHeader(point_format=3, version='1.2')
        if crs is not None:
            header.add_crs(CRS.from_user_input(crs))
        data = laspy.LasData(header)
        data.x, data.y, data.z = np.array(x), np.array(y), np.array(z)
        data.classification = np.array(classes, dtype=np.uint8)
        data.write(path)
        return path

    def test_raster_grid_spacing_matches_point_binning_for_fractional_extent(self):
        path = self.points([10, 11.1, 12.4], [20, 21.1, 22.4], [1, 2, 3], [2, 2, 2])
        product = LidarRasterizer(resolution=1, point_chunk_size=1).rasterize(path, ['dsm'])['dsm']
        self.assertEqual(product.array.shape, (3, 3))
        self.assertTrue(product.transform.almost_equals(from_origin(10, 22.4, 1, 1)))
        self.assertEqual(product.array[0, 2], 3)
        self.assertEqual(product.array[1, 1], 2)
        self.assertEqual(product.array[2, 0], 1)

    def test_single_point_grid_has_a_real_pixel_not_zero_resolution(self):
        path = self.points([10], [20], [3], [2])
        product = LidarRasterizer(resolution=2).rasterize(path, ['dsm'])['dsm']
        self.assertEqual(product.array.shape, (1, 1))
        self.assertEqual(product.transform, from_origin(10, 20, 2, 2))
        self.assertEqual(product.array[0, 0], 3)

    def test_explicit_empty_class_filter_keeps_noise_points(self):
        path = self.points([10, 10.1], [20, 20.1], [3, 9], [2, 7])
        filtered = LidarRasterizer().rasterize(path, ['dsm'])['dsm']
        unfiltered = LidarRasterizer(exclude_classes=[]).rasterize(path, ['dsm'])['dsm']
        self.assertEqual(np.nanmax(filtered.array), 3)
        self.assertEqual(np.nanmax(unfiltered.array), 9)

    def test_rasterizer_uses_reviewed_fallback_only_when_header_has_no_crs(self):
        path = self.points([10], [20], [3], [2], crs=None)
        product = LidarRasterizer().rasterize(
            path,
            ['dsm'],
            fallback_crs='EPSG:6342',
        )['dsm']
        self.assertEqual(CRS.from_wkt(product.crs_wkt).to_epsg(), 6342)

        with self.assertRaisesRegex(RuntimeError, 'No CRS found in LAZ header'):
            LidarRasterizer().rasterize(path, ['dsm'])

    def test_rasterizer_prefers_header_crs_over_fallback(self):
        path = self.points([10], [20], [3], [2], crs='EPSG:32618')
        product = LidarRasterizer().rasterize(
            path,
            ['dsm'],
            fallback_crs='EPSG:6342',
        )['dsm']
        self.assertEqual(CRS.from_wkt(product.crs_wkt).to_epsg(), 32618)

    def test_lidar_materialize_passes_planned_source_crs_to_rasterizer(self):
        path = self.points([10], [20], [3], [2], crs=None)
        source = USGSLidarSource(
            output_products=['dsm'],
            project_catalog_path=CATALOG,
        )
        report = AcquisitionReport.for_regions(['0'])
        report.add_asset(
            LocalAsset(
                region_id='0',
                asset_id='tile',
                path=path,
                kind='point_cloud',
                product='laz',
                metadata={'source_crs': 'EPSG:6342'},
            ),
            AssetStatus.SUCCESS,
        )

        result = source.materialize(
            report,
            RuntimeContext(ROOT, HTTPDownloadService()),
            AcquireOptions(output_dir=self.root),
        )

        self.assertEqual(result.summary()['success'], 1)
        with rasterio.open(result.regions['0'].success[0].path) as dataset:
            self.assertEqual(dataset.crs.to_epsg(), 6342)


if __name__ == '__main__':
    unittest.main()
