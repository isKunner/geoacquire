#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: test_core.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Offline tests for class_path construction, HTTP acquisition, regions, and tile planning.

import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from collections import OrderedDict
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from unittest.mock import patch

import laspy
import numpy as np
import rasterio
from pyproj import CRS
from rasterio.transform import from_origin

from geoacquire.core.config import apply_overrides, build_parser, load_config, load_raw_config
from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import (
    AcquisitionReport,
    AcquireOptions,
    AssetKind,
    AssetSpec,
    AssetStatus,
    DownloadRequest,
    LocalAsset,
    ProductType,
)
from geoacquire.core.pipeline import PipelineRunner
from geoacquire.postprocess.raster_align import RasterAlignToTargetPostprocessor
from geoacquire.regions.reader import read_raster_region
from geoacquire.services.http_download import HTTPDownloadService, _TransferResult
from geoacquire.sources.copdem.client import CopDEMClient
from geoacquire.sources.copdem.planner import plan_region_tiles
from geoacquire.sources.copdem.public_cog import CopDEMPublicCOGSource
from geoacquire.sources.rs.google import GoogleSource
from geoacquire.sources.usgs.lidar import USGSLidarSource, _LidarTile
from geoacquire.sources.usgs.lidar_catalog import LidarProjectCatalog
from geoacquire.sources.usgs.lidar_rasterizer import LidarRasterizer
from geoacquire.sources.usgs.wesm import WESMClient


class _Handler(BaseHTTPRequestHandler):
    payload = b'geoacquire-smoke-test'

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format_, *args):
        pass


class _BlockingHTTPDownloadService(HTTPDownloadService):
    """Test double that holds workers so request prefetch can be measured."""

    def __init__(self):
        self.release = threading.Event()

    def _download_one(self, request, output_dir, skip_existing, chunk_size):
        self.release.wait(timeout=5)
        path = os.path.join(output_dir, request.filename)
        return _TransferResult(request, 'success', path)


class _RetryHTTPDownloadService(HTTPDownloadService):
    """Test double that succeeds only on its third transfer attempt."""

    def __init__(self):
        self.attempts = 0

    def _download_one(self, request, output_dir, skip_existing, chunk_size):
        self.attempts += 1
        path = os.path.join(output_dir, request.filename)
        if self.attempts < 3:
            return _TransferResult(request, 'failed', path, 'temporary failure')
        return _TransferResult(request, 'success', path)


class GeoAcquireCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Own only this unique directory; never erase a pre-existing user's
        # .test_tmp directory or another concurrently running test process.
        temporary = tempfile.TemporaryDirectory(prefix='.test_core_', dir=cls.project_root)
        cls.addClassCleanup(temporary.cleanup)
        cls.temp_root = temporary.name
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_class_path_pipeline_and_http_service(self):
        port = self.server.server_address[1]
        raw = {
            'region': {
                'class_path': 'geoacquire.regions.bounds.BoundsRegionProvider',
                'init_args': {'source': [0.0, 0.0, 0.5, 0.5], 'region': 'test'},
            },
            'pipelines': [{
                'name': 'local_http',
                'source': {
                    'class_path': 'tests.fakes.StaticHTTPSource',
                    'init_args': {'url': f'http://127.0.0.1:{port}/asset'},
                },
                'acquire': {'output_dir': os.path.join(self.temp_root, 'output')},
                'postprocess': [{
                    'class_path': 'geoacquire.postprocess.print_step.PrintPostprocessor',
                    'init_args': {'message': 'offline smoke test'},
                }],
            }],
        }
        parser = build_parser()
        config = parser.instantiate(parser.parse_object(raw))
        context = RuntimeContext(self.project_root, HTTPDownloadService())
        reports = PipelineRunner(context).run(config.region, config.pipelines)
        path = reports['local_http'].regions['test'].success[0].path
        with open(path, 'rb') as file:
            self.assertEqual(file.read(), _Handler.payload)

    def test_english_and_chinese_defaults_have_identical_values(self):
        english = load_raw_config([
            os.path.join(self.project_root, 'configs', 'default.yaml'),
        ])
        chinese = load_raw_config([
            os.path.join(self.project_root, 'configs', 'default_zh.yaml'),
        ])
        self.assertEqual(english, chinese)
        self.assertEqual(
            english['region']['init_args']['exclude_suffixes'],
            [
                'lidar', 'dsm', 'dtm', 'usgs_dem_1m', 'nz_dem_1m', 'cnig_mdt50cm',
                'google', 'wayback', 'copdem', 'cop',
            ],
        )
        pipelines = {item['name']: item for item in english['pipelines']}
        self.assertEqual(
            pipelines['usgs_dem_1m']['source']['init_args']['filename_suffix'],
            'usgs_dem_1m',
        )
        self.assertEqual(
            pipelines['copdem']['source']['init_args']['filename_suffix'],
            'copdem',
        )

    def test_target_raster_region_metadata(self):
        target = os.path.join(self.temp_root, 'target.tif')
        self._write_target(target, 100.0)

        raw = {
            'region': {
                'class_path': 'geoacquire.regions.bounds.BoundsRegionProvider',
                'init_args': {'source': target},
            },
            'pipelines': [],
        }
        parser = build_parser()
        config = parser.instantiate(parser.parse_object(raw))
        region = next(iter(config.region.get_regions().values()))
        self.assertEqual(region.metadata['target_path'], target)
        self.assertEqual(region.metadata['shape'], (3, 4))

    def test_null_acquire_output_uses_target_parent_or_workspace_output(self):
        from geoacquire.core.models import Region

        context = RuntimeContext(self.temp_root, HTTPDownloadService())
        target_dir = os.path.join(self.temp_root, 'GT', 'AL')
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, '0.tif')
        self._write_target(target, -88.0)
        target_region = read_raster_region(target, '0')

        self.assertEqual(
            context.resolve_acquire_output(None, {'0': target_region}),
            target_dir,
        )
        self.assertEqual(
            context.resolve_acquire_output(
                None,
                {'bbox': Region('bbox', (-88.0, 30.0, -87.0, 31.0))},
            ),
            os.path.join(self.temp_root, 'output'),
        )

    def test_null_acquire_output_rejects_multiple_target_directories(self):
        first_dir = os.path.join(self.temp_root, 'GT', 'AL')
        second_dir = os.path.join(self.temp_root, 'GT', 'TX')
        os.makedirs(first_dir, exist_ok=True)
        os.makedirs(second_dir, exist_ok=True)
        first = os.path.join(first_dir, '0.tif')
        second = os.path.join(second_dir, '0.tif')
        self._write_target(first, -88.0)
        self._write_target(second, -98.0)
        regions = {
            'AL__0': read_raster_region(first, 'AL__0'),
            'TX__0': read_raster_region(second, 'TX__0'),
        }

        context = RuntimeContext(self.temp_root, HTTPDownloadService())
        with self.assertRaisesRegex(ValueError, 'set acquire.output_dir explicitly'):
            context.resolve_acquire_output(None, regions)

    def test_region_rejects_non_finite_bounds_before_spatial_query(self):
        from geoacquire.core.models import Region

        with self.assertRaisesRegex(ValueError, 'finite values'):
            Region('invalid', (0.0, 0.0, float('inf'), 1.0))

    def test_target_raster_directory_creates_one_region_per_file(self):
        directory = os.path.join(self.temp_root, 'targets')
        os.makedirs(directory, exist_ok=True)
        self._write_target(os.path.join(directory, 'a.tif'), 100.0)
        self._write_target(os.path.join(directory, 'b.tif'), 101.0)
        raw = {
            'region': {
                'class_path': 'geoacquire.regions.bounds.BoundsRegionProvider',
                'init_args': {'source': directory},
            },
            'pipelines': [],
        }
        parser = build_parser()
        config = parser.instantiate(parser.parse_object(raw))
        self.assertEqual(list(config.region.get_regions()), ['a', 'b'])

    def test_target_directory_does_not_scan_subdirectories(self):
        directory = os.path.join(self.temp_root, 'flat_targets')
        nested = os.path.join(directory, 'nested')
        os.makedirs(nested, exist_ok=True)
        root_target = os.path.join(directory, 'root.tif')
        self._write_target(root_target, -88.0)
        self._write_target(os.path.join(nested, 'hidden.tif'), -75.0)

        raw = {
            'region': {
                'class_path': 'geoacquire.regions.bounds.BoundsRegionProvider',
                'init_args': {'source': directory},
            },
            'pipelines': [],
        }
        parser = build_parser()
        config = parser.instantiate(parser.parse_object(raw))
        regions = config.region.get_regions()
        self.assertEqual(list(regions), ['root'])
        self.assertEqual(regions['root'].metadata['target_path'], root_target)

    def test_directory_snapshot_checks_completed_suffixes_without_path_isfile(self):
        from geoacquire.regions.bounds import BoundsRegionProvider

        directory = os.path.join(self.temp_root, 'snapshot_targets')
        os.makedirs(directory, exist_ok=True)
        for name, left in (
            ('a.tif', 100.0),
            ('a_lidar.tif', 100.0),
            ('a_google.tif', 100.0),
            ('b.tif', 101.0),
            ('b_lidar.tif', 101.0),
        ):
            self._write_target(os.path.join(directory, name), left)

        provider = BoundsRegionProvider(
            source=directory,
            exclude_suffixes=[],
            skip_existing_suffixes=['lidar', 'google'],
        )
        with patch(
            'geoacquire.regions.bounds.os.path.isfile',
            side_effect=AssertionError('directory sibling checks must use the snapshot'),
        ) as is_file:
            regions = provider.get_regions()

        is_file.assert_not_called()
        self.assertEqual(list(regions), ['b'])

    def test_google_tile_planning_matches_reference_area(self):
        from geoacquire.core.models import Region

        region = Region('example', (-157.0442776, 21.1503038, -157.0405956, 21.1543840))
        requests = list(GoogleSource(zoom=18, output_format='image').build_requests({'example': region}))
        self.assertEqual(len(requests), 16)
        self.assertTrue(all(request.filename.endswith('_google.jpg') for request in requests))

    def test_lidar_catalog_plans_full_xy_without_remote_headers(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        source._fetch_text = lambda _: '\n'.join([
            'https://example.invalid/USGS_LPC_DE_Statewide_B23_4470004381500.laz',
            'https://example.invalid/USGS_LPC_DE_Statewide_B23_4485004383000.laz',
        ])
        region = Region(
            '0',
            (448600.0, 4381600.0, 448700.0, 4381700.0),
            'EPSG:32618',
        )
        workunit = {
            'workunit': 'DE_Statewide_1_B23',
            'project': 'DE_Statewide_B23',
            'lpc_link': 'https://example.invalid/de',
            'collect_end': '2024-01-01',
            'horiz_crs': '32618',
        }

        planned = list(source._build_batch_requests(
            {'0': region},
            {'0': [workunit]},
        ))

        self.assertEqual(
            [request.filename for request in planned],
            ['USGS_LPC_DE_Statewide_B23_4485004383000.laz'],
        )
        self.assertEqual(planned[0].metadata['project_rule'], 'DE_Statewide_B23')
        self.assertFalse(hasattr(source, '_read_remote_las_bounds'))

    def test_lidar_catalog_supports_independent_mgrs_token_scales(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        filenames = [
            'USGS_LPC_AL_17County_2020_B20_16SFB097687.laz',
            'USGS_LPC_AL_17County_2020_B20_16SFB112687.laz',
            'USGS_LPC_AL_17County_2020_B20_16SFB112689.laz',
        ]
        source._fetch_text = lambda _: '\n'.join(
            f'https://example.invalid/{filename}' for filename in filenames
        )
        region = Region(
            'alabama',
            (410600.0, 3687100.0, 410700.0, 3687200.0),
            'EPSG:32616',
        )
        workunit = {
            'workunit': 'AL_17Co_2_2020',
            'project': 'AL_17County_2020_B20',
            'lpc_link': 'https://example.invalid/al',
            'collect_end': '2020-01-01',
            'horiz_crs': '32616',
        }

        planned = list(source._build_batch_requests(
            {'alabama': region},
            {'alabama': [workunit]},
        ))

        self.assertEqual([request.filename for request in planned], [filenames[1]])
        self.assertEqual(planned[0].metadata['tile_naming'], 'mgrs_tokens')

    def test_lidar_catalog_contains_reviewed_state_rules(self):
        catalog = LidarProjectCatalog(os.path.join(
            self.project_root,
            'configs',
            'usgs_lidar_projects.yaml',
        ))
        expected = {
            'HI_Hawaii_Island_2017': 'HI_Hawaii_Island_2017',
            'AL_17Co_2_2020': 'AL_17County_2_2020',
            'AR_NorthEast_1_D22': 'AR_NorthEast_D22',
            'AR_NRCS_Cache_2011': 'AR_NRCS_Languille_Cache_2011',
            'TX_CentralEast_1_A23': 'TX_CentralEast_1_A23',
            'TX_CentralEast_2_A23': 'TX_standard_1500m_MGRS',
            'TX_MiddleBrazos_2016': 'TX_MiddleBrazos_2016',
            'TX_Lower_SanBernard_B6_2017': 'TX_Lower_SanBernard_B6_2017',
            'USGS_LPC_TX_South_B6_2018_LAS_2019': 'TX_South_B6_2018',
            'WV_FEMAR3_Southcentral_B3_2018': 'WV_FEMAR3_Southcentral_B3_2018',
            'PA_WesternPA_1_2019': 'PA_WesternPA_1_2019',
            'WY_FEMA_East_B3_2019': 'WY_FEMA_East_min_min',
            'WY_GrandTetonNP_1_D22': 'WY_GrandTetonNP_1_D22',
            'WY_SouthCentral_5_2020': 'WY_SouthCentral_5_2020',
            'WY_Carbon_2015': 'WY_Carbon_2015',
        }
        for workunit, rule_name in expected.items():
            with self.subTest(workunit=workunit):
                rule = catalog.find({'workunit': workunit})
                self.assertIsNotNone(rule)
                self.assertEqual(rule.name, rule_name)

        expected_by_project = (
            ('AR_Western_4_B24', 'AR_Western_B24', 'AR_Western_Eastern_1km_MGRS'),
            ('AR_Eastern_7_D23', 'AR_Eastern_D23', 'AR_Western_Eastern_1km_MGRS'),
            ('AR_Ouachita_B3_2016', 'AR_Ouachita_FEMA_R6_Lidar_2016_D17', 'AR_Ouachita_2016'),
            ('AR_North_Corridor_B2_2017', 'AR_North_Corridor_FEMA_R6_Lidar_2016_D17', 'AR_North_Corridor_2017'),
            ('AR_DardanelleReservoir_2015', 'FEMA_R6_AR_Dardanelle_Reservoir_QL2_Lidar', 'AR_standard_1500m_MGRS'),
            ('MA_Western_1_B24', 'MA_Western_B24', 'MA_modern_1500m_MGRS_tokens'),
            ('MA_CentralEastern_1_2021', 'MA_CentralEastern_2021_B21', 'MA_modern_1500m_MGRS_tokens'),
            ('MA_CentralEastern_2_2021', 'MA_CentralEastern_2021_B21', 'MA_modern_1500m_MGRS_tokens'),
            ('CT_Statewide_B5_2016', 'CT_Statewide_C16', 'CT_Statewide_C16_quadrants'),
            ('CT_2023Statewide_2_C25', 'CT_2023Statewide_C25', 'CT_2023Statewide_C25_quadrants'),
            ('NY_SE4County_1_A22', 'NY_SouthEast4County_A22', 'NY_SouthEast4County_A22'),
            ('ME_Eastern_B1_2017', 'ME_EasternME_2017_A17', 'ME_north_1500m_tokens'),
            ('ME_MidCoast_2_2021', 'ME_MidCoast_2021_B21', 'ME_south_1500m_tokens'),
            ('ME_MidCentral_1_B23', 'ME_MidCentral_B23', 'ME_MidCentral_B23_1km_tokens'),
            ('MD_VA_NCB_KGeorge_1_2020', 'MD_VA_NorthChesapeakeBay_KGeorge_2020_D20', 'MD_VA_NCB_KGeorge_2020'),
            ('LA_CPRA_B3_2019', 'LA_CPRA_2019_C20', 'LA_standard_1km_MGRS'),
            ('LA_NortheastDOTD_3_2017', 'LA_NortheastDOTD_2017_C20', 'LA_NortheastDOTD_1500m_MGRS'),
        )
        for workunit, project, rule_name in expected_by_project:
            with self.subTest(workunit=workunit, project=project):
                rule = catalog.find({'workunit': workunit, 'project': project})
                self.assertIsNotNone(rule)
                self.assertEqual(rule.name, rule_name)

        # These examples contain only local serial/grid codes. The old logs did
        # not establish a coordinate transform, so formal planning must not guess.
        unresolved = (
            'TX_Central_B1_2017',
            'USGS_LPC_TX_Central_B2_2017_LAS_2019',
            'WV_FEMA_R3_East_unreviewed',
            'WY_GoshenCounty_B1_2017',
        )
        for workunit in unresolved:
            with self.subTest(unresolved=workunit):
                self.assertIsNone(catalog.find({'workunit': workunit}))

    def test_middle_brazos_rule_covers_live_header_bounds(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        workunit = {
            'workunit': 'TX_MiddleBrazos_2016',
            'project': 'Middle_Brazos_Lake_Whitney_TX_QL2_Lidar_FY15',
            'horiz_crs': '6343',
        }
        # Bounds came from bounded Range reads of the official LAZ files. The
        # samples cover different MGRS squares and both grid remainder values.
        cases = (
            ('14SPA0649', (606627.06, 3549763.65, 607500.0, 3550499.99)),
            ('14RPA2140', (621418.41, 3540500.19, 622499.99, 3541500.0)),
            ('14SPB3200', (631500.01, 3600000.0, 631914.51, 3600668.61)),
            ('14SNA8899', (588000.0, 3598500.01, 589500.0, 3599999.99)),
            ('14SPA1473', (613500.01, 3573000.01, 614999.99, 3574499.99)),
            ('14SNA7082', (570000.0, 3582000.0, 571500.0, 3583500.0)),
            ('14SNA7579', (574500.01, 3579000.0, 575999.99, 3580500.0)),
        )
        rule = source.catalog.find(workunit)
        self.assertIsNotNone(rule)
        for suffix, header_bounds in cases:
            with self.subTest(suffix=suffix):
                filename = f'USGS_LPC_Middle_Brazos_Lake_Whitney_TX_QL2_Lidar_FY15_{suffix}.laz'
                tile = source._parse_rule_tile(
                    filename,
                    'https://example.invalid/' + filename,
                    workunit,
                    rule,
                    [],
                )
                self.assertIsNotNone(tile)
                self.assertLessEqual(tile.bounds[0], header_bounds[0])
                self.assertLessEqual(tile.bounds[1], header_bounds[1])
                self.assertGreaterEqual(tile.bounds[2], header_bounds[2])
                self.assertGreaterEqual(tile.bounds[3], header_bounds[3])

    def test_arkansas_rules_cover_calibration_header_bounds(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        # Header bounds came from small Range reads of representative official
        # LAZ files. A rule footprint may be deliberately larger due to padding,
        # but it must never exclude the observed physical file footprint.
        cases = (
            (
                {'workunit': 'AR_Western_4_B24', 'project': 'AR_Western_B24', 'horiz_crs': '6344'},
                'USGS_LPC_AR_Western_B24_15SUU880900.laz',
                (388000.01, 3890000.01, 388999.99, 3890999.99),
            ),
            (
                {'workunit': 'AR_NorthEast_1_D22', 'project': 'AR_NorthEast_D22', 'horiz_crs': '6344'},
                'USGS_LPC_AR_NorthEast_D22_15S_XU_6555_3837.laz',
                (655500.01, 3837000.0, 656981.21, 3838499.99),
            ),
            (
                {'workunit': 'MO_Southwest_1_2021', 'project': 'MO_Southwest_2021_D21', 'horiz_crs': '6344'},
                'USGS_LPC_MO_Southwest_2021_D21_15SVB419103.laz',
                (418500.0, 4102500.0, 419999.99, 4103999.99),
            ),
            (
                {'workunit': 'MS_MississippiDelta_4_2018', 'project': 'MS_Central_Delta_2018_D18', 'horiz_crs': '6350'},
                'USGS_LPC_MS_Central_Delta_2018_D18_e0526n1262.laz',
                (526000.0, 1262000.0, 526999.99, 1262999.99),
            ),
            (
                {'workunit': 'AR_Ouachita_2016', 'project': 'AR_Ouachita_FEMA_R6_Lidar_2016_D17', 'horiz_crs': '6344'},
                'USGS_LPC_AR_Ouachita_FEMA_R6_Lidar_2016_D17_15SVT965860.laz',
                (496500.0, 3786000.0, 497999.99, 3787499.99),
            ),
            (
                {'workunit': 'AR_NRCS_A4_2016', 'project': 'AR_NRCS_AR_LiDAR_2016_B16', 'horiz_crs': '26915'},
                'USGS_LPC_AR_NRCS_AR_LiDAR_2016_B16_15SXV4529.laz',
                (645000.0, 3928500.01, 645121.87, 3929802.71),
            ),
            (
                {'workunit': 'AR_North_Corridor_B3_2017', 'project': 'AR_North_Corridor_FEMA_R6_Lidar_2016_D17', 'horiz_crs': '6344'},
                'USGS_LPC_AR_North_Corridor_FEMA_R6_Lidar_2016_D17_15SXV661978.laz',
                (661500.0, 3978000.0, 662196.0, 3979489.53),
            ),
            (
                {'workunit': 'AR_DardanelleReservoir_2015', 'project': 'FEMA_R6_AR_Dardanelle_Reservoir_QL2_Lidar', 'horiz_crs': '6344'},
                'USGS_LPC_FEMA_R6_AR_Dardanelle_Reservoir_QL2_Lidar_15swv190465.laz',
                (519000.005, 3946500.005, 520499.985, 3947999.985),
            ),
            (
                {'workunit': 'AR_LakeConwayPointRE_2014', 'project': 'NRCS_LakeConwayPointRemove', 'horiz_crs': '26915'},
                'USGS_LPC_NRCS_LakeConwayPointRemove_15_05763877.laz',
                (576000.0, 3877500.0, 577499.99, 3877915.48),
            ),
            (
                {'workunit': 'AR_NRCS_Languille_2011', 'project': 'NRCS_LAnguille', 'horiz_crs': '26915'},
                'USGS_LPC_NRCS_LAnguille_15_06973927.laz',
                (697500.01, 3927000.0, 699000.0, 3928499.99),
            ),
        )
        for workunit, filename, header_bounds in cases:
            with self.subTest(workunit=workunit['workunit']):
                rule = source.catalog.find(workunit)
                self.assertIsNotNone(rule)
                tile = source._parse_rule_tile(
                    filename,
                    'https://example.invalid/' + filename,
                    workunit,
                    rule,
                    [],
                )
                self.assertIsNotNone(tile)
                self.assertLessEqual(tile.bounds[0], header_bounds[0])
                self.assertLessEqual(tile.bounds[1], header_bounds[1])
                self.assertGreaterEqual(tile.bounds[2], header_bounds[2])
                self.assertGreaterEqual(tile.bounds[3], header_bounds[3])

    def test_massachusetts_rules_cover_calibration_header_bounds(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        # These bounds came from small Range reads of official LAZ files. The
        # samples include both already-aligned and half-step grid positions.
        cases = (
            ('MA_Western_1_B24', 'MA_Western_B24', '6347', '18TXN699701', (699000.01, 4701000.0, 700500.0, 4702499.99)),
            ('MA_Western_1_B24', 'MA_Western_B24', '6347', '18TYM706669', (706500.01, 4669500.0, 708000.0, 4670999.99)),
            ('MA_Western_1_B24', 'MA_Western_B24', '6347', '18TYM717698', (717000.01, 4698000.0, 718500.0, 4699499.99)),
            ('MA_CentralEastern_1_2021', 'MA_CentralEastern_2021_B21', '6348', '19TBG295674', (295500.0, 4674000.0, 296999.99, 4675499.99)),
            ('MA_CentralEastern_1_2021', 'MA_CentralEastern_2021_B21', '6348', '19TBH265725', (265500.0, 4725000.0, 266999.99, 4726499.99)),
            ('MA_CentralEastern_1_2021', 'MA_CentralEastern_2021_B21', '6348', '19TCG330666', (330000.0, 4666500.0, 331499.99, 4667999.99)),
            ('MA_CentralEastern_2_2021', 'MA_CentralEastern_2021_B21', '6348', '19TCG321609', (321000.0, 4609500.0, 322499.99, 4610999.99)),
            ('MA_CentralEastern_2_2021', 'MA_CentralEastern_2021_B21', '6348', '19TCG364645', (364500.0, 4645500.0, 365999.99, 4646999.99)),
            ('MA_CentralEastern_2_2021', 'MA_CentralEastern_2021_B21', '6348', '19TCG312644', (312000.0, 4644000.0, 313499.99, 4645499.99)),
        )
        for workunit, project, crs, suffix, header_bounds in cases:
            with self.subTest(workunit=workunit, suffix=suffix):
                model = {'workunit': workunit, 'project': project, 'horiz_crs': crs}
                rule = source.catalog.find(model)
                filename = f'USGS_LPC_{project}_{suffix}.laz'
                tile = source._parse_rule_tile(
                    filename,
                    'https://example.invalid/' + filename,
                    model,
                    rule,
                    [],
                )
                self.assertIsNotNone(tile)
                self.assertLessEqual(tile.bounds[0], header_bounds[0])
                self.assertLessEqual(tile.bounds[1], header_bounds[1])
                self.assertGreaterEqual(tile.bounds[2], header_bounds[2])
                self.assertGreaterEqual(tile.bounds[3], header_bounds[3])

    def test_connecticut_rules_cover_calibration_header_bounds(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        cases = (
            ('CT_Statewide_B1_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_005665_nw.laz', (1005000.0, 667499.99, 1007499.99, 669999.98)),
            ('CT_Statewide_B1_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_800585_ne.laz', (802500.0, 587499.99, 804999.99, 589999.98)),
            ('CT_Statewide_B2_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_015710_nw.laz', (1015000.0, 712499.99, 1017499.98, 714999.98)),
            ('CT_Statewide_B3_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_000785_nw.laz', (1000000.0, 787499.99, 1002499.99, 789999.98)),
            ('CT_Statewide_B4_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_015845_sw.laz', (1015000.01, 844999.99, 1017499.99, 847499.98)),
            ('CT_Statewide_B5_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_000870_sw.laz', (1000000.0, 869999.99, 1002499.99, 872499.98)),
            ('CT_Statewide_B6_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_015925_nw.laz', (1015000.0, 927499.99, 1017499.99, 929999.98)),
            ('CT_Statewide_B7_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_005895_sw.laz', (1005000.0, 894999.99, 1007499.99, 897499.98)),
            ('CT_Statewide_B8_2016', 'CT_Statewide_C16', '6434', 'USGS_LPC_CT_Statewide_C16_090815_se.laz', (1092500.01, 815000.0, 1095000.0, 817499.99)),
            ('CT_2023Statewide_2_C25', 'CT_2023Statewide_C25', '6434', 'USGS_LPC_CT_2023Statewide_C25_800675_ne.laz', (802500.0, 677500.0, 805000.0, 680000.0)),
            ('CT_2023Statewide_2_C25', 'CT_2023Statewide_C25', '6434', 'USGS_LPC_CT_2023Statewide_C25_815675_se.laz', (817500.0, 675000.0, 820000.0, 677500.0)),
            ('CT_2023Statewide_2_C25', 'CT_2023Statewide_C25', '6434', 'USGS_LPC_CT_2023Statewide_C25_800710_nw.laz', (800000.0, 712500.0, 802500.0, 715000.0)),
            ('NY_SE4County_1_A22', 'NY_SouthEast4County_A22', '6347', 'USGS_LPC_NY_SouthEast4County_A22_u_5460057200_2022.laz', (546000.001, 4572000.002, 547500.0, 4573499.998)),
            ('NY_SE4County_1_A22', 'NY_SouthEast4County_A22', '6347', 'USGS_LPC_NY_SouthEast4County_A22_u_5535060500_2022.laz', (553500.001, 4605000.001, 554999.999, 4606499.998)),
            ('NY_SE4County_1_A22', 'NY_SouthEast4County_A22', '6347', 'USGS_LPC_NY_SouthEast4County_A22_u_5415062000_2022.laz', (541500.001, 4620000.002, 543000.0, 4621499.999)),
        )
        for workunit, project, crs, filename, header_bounds in cases:
            with self.subTest(workunit=workunit, filename=filename):
                model = {'workunit': workunit, 'project': project, 'horiz_crs': crs}
                rule = source.catalog.find(model)
                tile = source._parse_rule_tile(
                    filename,
                    'https://example.invalid/' + filename,
                    model,
                    rule,
                    [],
                )
                self.assertIsNotNone(tile)
                self.assertLessEqual(tile.bounds[0], header_bounds[0])
                self.assertLessEqual(tile.bounds[1], header_bounds[1])
                self.assertGreaterEqual(tile.bounds[2], header_bounds[2])
                self.assertGreaterEqual(tile.bounds[3], header_bounds[3])

    def test_maine_maryland_louisiana_rules_cover_calibration_header_bounds(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        # Representative bounds were read from official LAZ headers with HTTP
        # Range requests. Together they exercise every new grid/parser variant.
        cases = (
            ('ME_CrownOfMaine_B1_2018', 'ME_CrownofMaine_2018_A18', '6348', 'USGS_LPC_ME_CrownofMaine_2018_A18_19TEM522146.laz', (522000.0, 5146500.0, 523499.99, 5148000.0)),
            ('ME_Eastern_B1_2017', 'ME_EasternME_2017_A17', '6348', 'USGS_LPC_ME_EasternME_2017_A17_19TDL486077_LAS_2019.laz', (486000.0, 5077500.0, 487499.99, 5078999.99)),
            ('ME_WesternMtns_1_B24', 'ME_WesternMtns_B24', '6348', 'USGS_LPC_ME_WesternMtns_B24_19TDL451082.laz', (451500.0, 5082000.0, 452999.99, 5083499.99)),
            ('ME_MidCoast_2_2021', 'ME_MidCoast_2021_B21', '6348', 'USGS_LPC_ME_MidCoast_2021_B21_19TFK603927.laz', (603000.01, 4927500.0, 604500.0, 4928999.99)),
            ('ME_SouthCentral_1_B22', 'ME_SouthCentral_B22', '6348', 'USGS_LPC_ME_SouthCentral_B22_19TCJ381837.laz', (381000.0, 4837500.0, 382499.99, 4838999.99)),
            ('ME_SouthCoastal_1_2020', 'ME_SouthCoastal_2020_A20', '6348', 'USGS_LPC_ME_SouthCoastal_2020_A20_19TCH361782.laz', (361500.0, 4782000.0, 362999.99, 4783499.99)),
            ('ME_MidCentral_1_B23', 'ME_MidCentral_B23', '6348', 'USGS_LPC_ME_MidCentral_B23_19TDK433946.laz', (433000.0, 4946000.0, 433999.99, 4946999.99)),
            ('MD_4County_1_D24', 'MD_4County_D24', '6346', 'USGS_LPC_MD_4County_D24_17sod440530.laz', (644000.0, 4353000.0, 645000.0, 4354000.0)),
            ('MD_4County_2_D24', 'MD_4County_D24', '6347', 'USGS_LPC_MD_4County_D24_18suj260640.laz', (326000.0, 4364000.0, 327000.0, 4365000.0)),
            ('MD_VA_NCB_KGeorge_1_2020', 'MD_VA_NorthChesapeakeBay_KGeorge_2020_D20', '6347', 'USGS_LPC_MD_VA_NorthChesapeakeBay_KGeorge_2020_D20_18suj825095.laz', (382500.0, 4309500.0, 383999.99, 4310999.99)),
            ('MD_VA_Sandy_NCR_2014', 'MD_VA_Sandy_NCR_2014', '6347', 'USGS_LPC_MD_VA_Sandy_NCR_2014_18SUH334288.laz', (334500.0, 4288500.0, 335999.99, 4289999.99)),
            ('MD_Western_1_D21', 'MD_Western_D21', '6346', 'USGS_LPC_MD_Western_D21_17spd335920.laz', (733500.0, 4392000.0, 735000.0, 4393499.99)),
            ('MD_Western_1_D21', 'MD_Western_D21', '6346', 'USGS_LPC_MD_Western_2021_D21_17sod945815.laz', (694734.86, 4381648.67, 696000.0, 4383000.0)),
            ('MD_Western_2_D21', 'MD_Western_D21', '6347', 'USGS_LPC_MD_Western_D21_18suj000575.laz', (300000.0, 4357500.0, 301499.99, 4358999.99)),
            ('LA_Bayou_Nezpique_B1_2018', 'LA_Bayou_Nezpique_2018_D18', '6344', 'USGS_LPC_LA_Bayou_Nezpique_2018_D18_15RWQ3820.laz', (538000.0, 3420000.0, 538999.99, 3420999.99)),
            ('LA_CPRA_B3_2019', 'LA_CPRA_2019_C20', '6344', 'USGS_LPC_LA_CPRA_2019_C20_15SVR930990.laz', (493000.0, 3599000.0, 493999.99, 3599999.99)),
            ('LA_Catahoula_1_2017', 'LA_Catahoula_Concordia_2017_D17', '6344', 'USGS_LPC_LA_Catahoula_Concordia_2017_D17_15RXQ3282.laz', (632000.0, 3482000.001, 632999.998, 3482999.998)),
            ('LA_Sabine_River_Lidar_A1_2018', 'LA_Sabine_River_Lidar_2018_D18', '6344', 'USGS_LPC_LA_Sabine_River_Lidar_2018_D18_15RVQ0070.laz', (500000.0, 3470000.0, 500999.99, 3470999.99)),
            ('LA_Sabine_River_Lidar_A2_2018', 'LA_Sabine_River_Lidar_2018_D18', '6344', 'USGS_LPC_LA_Sabine_River_Lidar_2018_D18_15RVP3371.laz', (433000.0, 3371000.0, 433999.99, 3371999.99)),
            ('LA_Sabine_River_Lidar_A5_2018', 'LA_Sabine_River_Lidar_2018_D18', '6344', 'USGS_LPC_LA_Sabine_River_Lidar_2018_D18_15SVS3947.laz', (439000.0, 3647000.0, 439999.99, 3647999.99)),
            ('LA_NortheastDOTD_3_2017', 'LA_NortheastDOTD_2017_C20', '6344', 'USGS_LPC_LA_NortheastDOTD_2017_C20_15RWQ8372.laz', (583500.0, 3472500.0, 584999.99, 3473999.99)),
        )
        for workunit, project, crs, filename, header_bounds in cases:
            with self.subTest(workunit=workunit, filename=filename):
                model = {'workunit': workunit, 'project': project, 'horiz_crs': crs}
                rule = source.catalog.find(model)
                self.assertIsNotNone(rule)
                tile = source._parse_rule_tile(
                    filename,
                    'https://example.invalid/' + filename,
                    model,
                    rule,
                    [],
                )
                self.assertIsNotNone(tile)
                self.assertLessEqual(tile.bounds[0], header_bounds[0])
                self.assertLessEqual(tile.bounds[1], header_bounds[1])
                self.assertGreaterEqual(tile.bounds[2], header_bounds[2])
                self.assertGreaterEqual(tile.bounds[3], header_bounds[3])

    def test_lidar_axis_pair_rule_has_a_deterministic_footprint(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        workunit = {
            'workunit': 'WV_FEMAR3_Southcentral_B3_2018',
            'horiz_crs': '32617',
        }
        rule = source.catalog.find(workunit)
        tile = source._parse_rule_tile(
            'USGS_LPC_WV_example_E1234N5678.laz',
            'https://example.invalid/tile.laz',
            workunit,
            rule,
            [],
        )
        self.assertIsNotNone(tile)
        self.assertEqual(tile.bounds, (1234000.0, 5678000.0, 1235000.0, 5679000.0))

    def test_hawaii_island_rule_uses_workunit_crs_and_one_kilometer_tiles(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        workunit = {'workunit': 'HI_Hawaii_Island_2017', 'horiz_crs': '6635'}
        rule = source.catalog.find(workunit)
        tile = source._parse_rule_tile(
            'USGS_LPC_HI_Hawaii_Island_Lidar_NOAA_2017_B17_5QKC250180.laz',
            'https://example.invalid/tile.laz',
            workunit,
            rule,
            [],
        )
        self.assertEqual(tile.source_crs, 'EPSG:6635')
        self.assertEqual(tile.bounds, (225000.0, 2218000.0, 226000.0, 2219000.0))

    def test_lidar_audit_stops_before_download_tile_selection(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            mode='audit',
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        region = Region('0', (448600.0, 4381600.0, 448700.0, 4381700.0), 'EPSG:32618')
        workunit = {
            'workunit': 'DE_Statewide_1_B23',
            'project': 'DE_Statewide_B23',
            'lpc_link': 'https://example.invalid/de',
            'collect_end': '2024-01-01',
            'horiz_crs': '32618',
        }
        source.wesm.query_many = lambda _: {'0': [workunit]}
        source._fetch_text = lambda _: (
            'https://example.invalid/USGS_LPC_DE_Statewide_B23_4485004383000.laz'
        )
        source._select_tiles = lambda *args, **kwargs: self.fail('audit selected download tiles')

        source._audit_regions({'0': region})

        self.assertEqual(source._audit[0]['status'], 'matched')
        self.assertEqual(source._audit[0]['parsed_count'], 1)

    def test_lidar_unknown_project_is_audited_without_a_download_request(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            mode='audit',
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        source._fetch_text = lambda _: '\n'.join([
            'https://example.invalid/USGS_LPC_UNKNOWN_FF232.laz',
            'https://example.invalid/USGS_LPC_UNKNOWN_FF233.laz',
        ])
        region = Region('unknown', (0.0, 0.0, 1.0, 1.0), 'EPSG:4326')
        workunit = {
            'workunit': 'UNKNOWN_WORKUNIT',
            'project': 'UNKNOWN_PROJECT',
            'lpc_link': 'https://example.invalid/unknown',
            'collect_end': '2024-01-01',
            'horiz_crs': '4326',
        }

        planned = list(source._build_batch_requests(
            {'unknown': region},
            {'unknown': [workunit]},
        ))

        self.assertEqual(planned, [])
        self.assertEqual(source._audit[0]['status'], 'unmatched')
        self.assertEqual(source._audit[0]['unmatched_count'], 2)
        self.assertEqual(source._audit[0]['targets'][0]['region_id'], 'unknown')

    def test_lidar_missing_link_list_is_audited_and_does_not_abort_planning(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        source._fetch_text = lambda _: (_ for _ in ()).throw(
            RuntimeError('404 Client Error: Not Found')
        )
        region = Region('tx', (-97.1, 28.7, -97.0, 28.8))
        workunit = {
            'workunit': 'TX_VICTORIA_2006',
            'project': 'TX_VICTORIA_2006',
            'lpc_link': 'https://example.invalid/legacy/TX_VICTORIA_2006',
            'collect_end': '2006-01-01',
            'horiz_crs': '4326',
        }

        planned = list(source._build_batch_requests(
            {'tx': region},
            {'tx': [workunit]},
        ))

        self.assertEqual(planned, [])
        self.assertEqual(source._audit[0]['status'], 'unmatched')
        self.assertIn('404 Client Error', source._audit[0]['error'])

    def test_lidar_duplicate_destination_reuses_one_request_and_merges_targets(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        filename = 'USGS_LPC_AL_25_County_Lidar_2017_B17_16R_DU_1291.laz'
        first_tile = _LidarTile(
            'https://example.invalid/new/' + filename,
            filename,
            (0.0, 0.0, 1.0, 1.0),
            'EPSG:32616',
            'mgrs',
            'newer_rule',
        )
        duplicate_tile = _LidarTile(
            'https://example.invalid/old/' + filename,
            filename,
            (0.0, 0.0, 1.0, 1.0),
            'EPSG:32616',
            'mgrs',
            'older_rule',
        )
        first = source._requests_for_selected(
            OrderedDict([(first_tile.url, (first_tile, {'104'}))]),
        )
        second = source._requests_for_selected(
            OrderedDict([(duplicate_tile.url, (duplicate_tile, {'105'}))]),
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0].target_region_ids, ('104',))
        self.assertEqual(second[0].target_region_ids, ('105',))

    def test_lidar_link_list_deduplicates_repeated_filenames(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        filename = 'USGS_LPC_DE_Statewide_B23_4485004383000.laz'
        source._fetch_text = lambda _: '\n'.join((
            f'https://example.invalid/first/{filename}',
            f'https://example.invalid/second/{filename}',
            'https://example.invalid/USGS_LPC_DE_Statewide_B23_.laz',
        ))
        plan = source._load_workunit_plan({
            'workunit': 'DE_Statewide_1_B23',
            'project': 'DE_Statewide_B23',
            'lpc_link': 'https://example.invalid/de',
            'horiz_crs': '32618',
        })

        self.assertEqual(plan.filenames, 1)
        self.assertEqual(plan.duplicate_filenames, 1)
        self.assertEqual(plan.ignored_placeholders, 1)
        self.assertEqual(len(plan.tiles), 1)

    def test_lidar_shared_pool_downloads_one_tile_for_adjacent_targets(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        source._fetch_text = lambda _: (
            'https://example.invalid/USGS_LPC_DE_Statewide_B23_4485004383000.laz'
        )
        regions = {
            '0': Region('0', (448600.0, 4381600.0, 448700.0, 4381700.0), 'EPSG:32618'),
            '1': Region('1', (448700.0, 4381700.0, 448800.0, 4381800.0), 'EPSG:32618'),
        }
        workunit = {
            'workunit': 'DE_Statewide_1_B23',
            'project': 'DE_Statewide_B23',
            'lpc_link': 'https://example.invalid/de',
            'collect_end': '2024-01-01',
            'horiz_crs': '32618',
        }

        planned = list(source._build_batch_requests(
            regions,
            {'0': [workunit], '1': [workunit]},
        ))

        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].target_region_ids, ('0', '1'))

    def test_lidar_shared_asset_is_exposed_to_every_target(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        path = os.path.join(self.temp_root, 'shared.laz')
        report = AcquisitionReport.for_regions(['0', '1'])
        report.add_asset(
            LocalAsset(
                region_id='0',
                asset_id='shared',
                path=path,
                kind='point_cloud',
                product='laz',
                metadata={'target_region_ids': ('0', '1')},
            ),
            'success',
        )

        physical_result = source.materialize(
            report,
            RuntimeContext(self.project_root, HTTPDownloadService()),
            AcquireOptions(output_dir=self.temp_root),
        )

        self.assertEqual(physical_result.regions['0'].asset_count, 1)
        self.assertEqual(physical_result.regions['1'].asset_count, 0)
        progress = AcquisitionProgress(['0', '1'])
        progress.register('shared.laz', ['0', '1'])
        progress.close_all()
        progress.complete('shared.laz', physical_result)
        self.assertEqual(progress.report.regions['0'].success[0].path, path)
        self.assertEqual(progress.report.regions['1'].reused[0].path, path)

    def test_lidar_newer_workunit_covers_and_suppresses_the_older_one(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )

        def links(url):
            prefix = 'new' if '/new/' in url else 'old'
            return f'https://example.invalid/{prefix}_4485004383000.laz'

        source._fetch_text = links
        region = Region('0', (448600.0, 4381600.0, 448700.0, 4381700.0), 'EPSG:32618')
        common = {
            'workunit': 'DE_Statewide_1_B23',
            'project': 'DE_Statewide_B23',
            'horiz_crs': '32618',
        }
        newer = {**common, 'lpc_link': 'https://example.invalid/new', 'collect_end': '2024-01-01'}
        older = {**common, 'lpc_link': 'https://example.invalid/old', 'collect_end': '2020-01-01'}

        planned = list(source._build_batch_requests(
            {'0': region},
            {'0': [older, newer]},
        ))

        self.assertEqual([request.filename for request in planned], ['new_4485004383000.laz'])

    def test_lidar_queries_target_directory_once(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        calls = []
        source.wesm.query_many = lambda batch: calls.append(tuple(batch)) or {
            region_id: [] for region_id in batch
        }
        target_dir = os.path.join(self.temp_root, 'GT', 'AL')
        regions = {
            '0': Region(
                '0',
                (0.0, 0.0, 1.0, 1.0),
                metadata={'target_path': os.path.join(target_dir, '0.tif')},
            ),
            '1': Region(
                '1',
                (1.0, 0.0, 2.0, 1.0),
                metadata={'target_path': os.path.join(target_dir, '1.tif')},
            ),
        }

        self.assertEqual(list(source._build_requests(regions)), [])
        self.assertEqual(calls, [('0', '1')])

    def test_lidar_prefetches_the_next_workunit_while_current_urls_are_consumed(self):
        from geoacquire.core.models import Region

        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        next_started = threading.Event()
        release_next = threading.Event()

        def links(url):
            if '/old/' in url:
                next_started.set()
                release_next.wait(timeout=2)
                return 'https://example.invalid/old_4485004383000.laz'
            return 'https://example.invalid/new_4485004383000.laz'

        source._fetch_text = links
        region = Region('0', (448600.0, 4381600.0, 448700.0, 4381700.0), 'EPSG:32618')
        common = {
            'workunit': 'DE_Statewide_1_B23',
            'project': 'DE_Statewide_B23',
            'horiz_crs': '32618',
        }
        newer = {**common, 'lpc_link': 'https://example.invalid/new', 'collect_end': '2024-01-01'}
        older = {**common, 'lpc_link': 'https://example.invalid/old', 'collect_end': '2020-01-01'}
        requests = source._build_batch_requests(
            {'0': region},
            {'0': [newer, older]},
        )

        first = next(requests)
        self.assertEqual(first.filename, 'new_4485004383000.laz')
        self.assertTrue(next_started.wait(timeout=1))
        release_next.set()
        list(requests)

    def test_lidar_numeric_workunit_crs_falls_back_to_esri_authority(self):
        source = USGSLidarSource(
            output_products=['laz'],
            project_catalog_path=os.path.join(
                self.project_root,
                'configs',
                'usgs_lidar_projects.yaml',
            ),
        )
        crs = source._workunit_crs(
            {'workunit': 'AL_Clay_CleburneCo_2013', 'horiz_crs': '102629'}
        )
        self.assertIsNotNone(crs)
        self.assertEqual(crs.to_authority(), ('ESRI', '102629'))

    def test_wesm_query_treats_wgs84_and_nad83_envelopes_as_equivalent(self):
        from geoacquire.core.models import Region

        bounds = (-85.804, 33.325, -85.799, 33.331)
        region = Region('0', bounds, 'EPSG:9518')

        shape = WESMClient._region_shape(region, CRS.from_epsg(4269))

        self.assertEqual(shape.bounds, bounds)

    def test_lidar_chunked_rasterization_is_chunk_size_invariant(self):
        path = os.path.join(self.temp_root, 'chunked_points.las')
        header = laspy.LasHeader(point_format=3, version='1.2')
        header.add_crs(CRS.from_epsg(32618))
        points = laspy.LasData(header)
        points.x = [0.1, 0.2, 1.1, 1.2, 2.1, 2.2]
        points.y = [2.9, 2.8, 1.9, 1.8, 0.9, 0.8]
        points.z = [10.0, 12.0, 5.0, 7.0, 2.0, 3.0]
        points.classification = [2, 5, 2, 7, 2, 5]
        points.write(path)

        chunked = LidarRasterizer(point_chunk_size=2).rasterize(path, ['dsm', 'dtm'])
        single_chunk = LidarRasterizer(point_chunk_size=100).rasterize(path, ['dsm', 'dtm'])
        for product in ('dsm', 'dtm'):
            np.testing.assert_array_equal(
                chunked[product].array,
                single_chunk[product].array,
            )
            self.assertTrue(chunked[product].transform.almost_equals(single_chunk[product].transform))

    def test_http_download_consumes_requests_with_a_bounded_window(self):
        service = _BlockingHTTPDownloadService()
        yielded = []
        initial_window_ready = threading.Event()

        def requests():
            for index in range(40):
                yielded.append(index)
                if len(yielded) == 8:
                    initial_window_ready.set()
                yield DownloadRequest(
                    region_id='test',
                    asset_id=f'asset_{index}',
                    url=f'https://example.invalid/{index}',
                    filename=f'asset_{index}.bin',
                )

        state = {}

        def run_download():
            try:
                state['report'] = service.download(
                    requests(),
                    ['test'],
                    self.temp_root,
                    max_workers=2,
                    max_retries=0,
                )
            except Exception as exc:
                state['error'] = exc

        thread = threading.Thread(target=run_download)
        thread.start()
        self.assertTrue(initial_window_ready.wait(timeout=2))
        self.assertEqual(len(yielded), 8)
        service.release.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', state)
        self.assertEqual(
            state['report'].summary(),
            {'success': 40, 'skipped': 0, 'reused': 0, 'failed': 0},
        )

    def test_http_download_retries_inside_the_bounded_task(self):
        service = _RetryHTTPDownloadService()
        request = DownloadRequest(
            region_id='test',
            asset_id='retry_asset',
            url='https://example.invalid/retry',
            filename='retry.bin',
        )
        report = service.download(
            iter([request]),
            ['test'],
            self.temp_root,
            max_workers=1,
            max_retries=2,
            retry_delay=0,
        )
        self.assertEqual(service.attempts, 3)
        self.assertEqual(report.summary(), {'success': 1, 'skipped': 0, 'reused': 0, 'failed': 0})

    def test_http_duplicate_destination_reuses_first_transfer_without_aborting(self):
        port = self.server.server_address[1]
        filename = 'duplicate_destination.bin'
        requests = [
            DownloadRequest(
                region_id='104',
                asset_id='newer_workunit',
                url=f'http://127.0.0.1:{port}/newer',
                filename=filename,
            ),
            DownloadRequest(
                region_id='104',
                asset_id='older_workunit',
                url=f'http://127.0.0.1:{port}/older',
                filename=filename,
            ),
        ]

        output = StringIO()
        with redirect_stdout(output):
            report = HTTPDownloadService().download(
                requests,
                ['104'],
                os.path.join(self.temp_root, 'duplicate_output'),
                max_workers=1,
                max_retries=0,
            )

        self.assertEqual(report.summary(), {'success': 1, 'skipped': 0, 'reused': 0, 'failed': 0})
        self.assertIn('action=reuse', output.getvalue())
        self.assertIn('result=success', output.getvalue())
        self.assertIn('reused_duplicates=1', output.getvalue())

    def test_http_writes_directly_to_configured_directory(self):
        port = self.server.server_address[1]
        output_dir = os.path.join(self.temp_root, 'flat_output')
        request = DownloadRequest(
            region_id='region_that_must_not_become_a_directory',
            asset_id='flat_asset',
            url=f'http://127.0.0.1:{port}/flat',
            filename='flat.bin',
        )

        report = HTTPDownloadService().download(
            [request],
            [request.region_id],
            output_dir,
            max_retries=0,
        )

        path = report.regions[request.region_id].success[0].path
        self.assertEqual(path, os.path.join(output_dir, 'flat.bin'))
        self.assertTrue(os.path.isfile(path))

    def test_copdem_exact_maximum_edge_is_half_open(self):
        from geoacquire.core.models import Region

        region = Region('example', (111.0, 41.0, 112.0, 42.0))
        self.assertEqual(plan_region_tiles(region, '30'), ['Copernicus_DSM_10_N41_00_E111_00_DEM'])

    def test_copdem_catalog_search_selects_latest_delivery_with_stable_tie_break(self):
        client = CopDEMClient('offline-user', 'offline-password')
        with (
            patch.object(client, 'ensure_token'),
            patch.object(client.session, 'get') as request,
        ):
            request.return_value.json.return_value = {
                'value': [
                    {
                        'Id': 'old-id',
                        'Attributes': [{'Name': 'dataset', 'Value': 'COP-DEM_GLO-30-DGED/2023_1'}],
                    },
                    {
                        'Id': 'latest-z',
                        'Attributes': [{'Name': 'dataset', 'Value': 'COP-DEM_GLO-30-DGED/2024_1'}],
                    },
                    {
                        'Id': 'latest-a',
                        'Attributes': [{'Name': 'dataset', 'Value': 'COP-DEM_GLO-30-DGED/2024_1'}],
                    },
                ]
            }
            result = client.search('Copernicus_DSM_10_S09_00_W036_00_DEM')

        self.assertEqual(result, 'latest-a')
        params = request.call_args.kwargs['params']
        self.assertIn("att:att/Name eq 'gridId'", params['$filter'])
        self.assertIn("Value eq 'S09_W036'", params['$filter'])
        self.assertEqual(params['$top'], 100)
        self.assertEqual(params['$select'], 'Id,Attributes')
        self.assertEqual(params['$expand'], 'Attributes')

    def test_copdem_public_cog_deduplicates_tiles_across_regions(self):
        from geoacquire.core.models import Region

        source = CopDEMPublicCOGSource('30')
        requests = list(
            source.build_requests(
                {
                    'a': Region('a', (-35.9, -8.9, -35.8, -8.8)),
                    'b': Region('b', (-35.7, -8.7, -35.6, -8.6)),
                }
            )
        )

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.target_region_ids, ('a', 'b'))
        self.assertEqual(request.product, ProductType.DEM)
        self.assertEqual(request.kind, AssetKind.RASTER)
        self.assertEqual(
            request.filename,
            'Copernicus_DSM_COG_10_S09_00_W036_00_DEM.tif',
        )
        self.assertEqual(
            request.url,
            'https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/'
            'Copernicus_DSM_COG_10_S09_00_W036_00_DEM/'
            'Copernicus_DSM_COG_10_S09_00_W036_00_DEM.tif',
        )

    def test_copdem_archive_extraction(self):
        tile = 'Copernicus_DSM_10_N41_00_E111_00_DEM'
        archive_path = os.path.join(self.temp_root, 'product.zip')
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr(f'product/{tile}.tif', b'fake-geotiff')
        extracted = CopDEMClient.extract(archive_path, tile, self.temp_root)
        self.assertEqual(os.path.basename(extracted), f'{tile}_copdem.tif')
        with open(extracted, 'rb') as file:
            self.assertEqual(file.read(), b'fake-geotiff')

    def test_named_pipeline_merge_and_command_line_override(self):
        default_path = os.path.join(self.project_root, 'configs', 'default.yaml')
        example_path = os.path.join(self.project_root, 'configs', 'examples', 'target_raster.yaml')
        config = load_config(
            [default_path, example_path],
            [
                'pipelines.google.enable=true',
                'pipelines.google.source.init_args.zoom=17',
                'pipelines.google.acquire.max_workers=2',
            ],
        )
        self.assertEqual(len(config.pipelines), 8)
        enabled = [pipeline for pipeline in config.pipelines if pipeline.enable]
        self.assertEqual([pipeline.name for pipeline in enabled], ['google'])
        self.assertEqual(enabled[0].source.zoom, 17)
        self.assertEqual(enabled[0].acquire.max_workers, 2)

        command = [
            sys.executable,
            '-B',
            os.path.join(self.project_root, 'run.py'),
            '-c',
            default_path,
            '-c',
            example_path,
            '--set',
            'pipelines.google.enable=true',
            '--check',
        ]
        completed = subprocess.run(command, cwd=self.project_root, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('name=google enable=True', completed.stdout)

    def test_geodar_runtime_overlay_reuses_default_postprocessor(self):
        config = load_config([
            os.path.join(self.project_root, 'configs', 'default.yaml'),
            os.path.join(self.project_root, 'configs', 'runs', 'geodar_state_lidar.yaml'),
        ])
        lidar = next(item for item in config.pipelines if item.name == 'usgs_lidar')
        self.assertTrue(lidar.enable)
        self.assertEqual(lidar.source.output_products, ['dsm'])
        self.assertEqual(lidar.acquire.max_workers, 4)
        self.assertEqual(len(lidar.postprocess), 1)
        self.assertIsNone(lidar.acquire.output_dir)
        self.assertIsNone(lidar.postprocess[0].output_dir)
        self.assertEqual(lidar.postprocess[0].input_products, {'dsm'})
        self.assertIsNone(config.region.skip_existing_suffixes)
        self.assertIn('dsm', config.region.exclude_suffixes)
        self.assertEqual(config.reporting.json_path, './logs/geodar_AL_run.json')
        self.assertEqual(config.reporting.log_path, './logs/geodar_AL.log')
        self.assertFalse(config.reporting.overwrite)

        audit = load_config([
            os.path.join(self.project_root, 'configs', 'default.yaml'),
            os.path.join(self.project_root, 'configs', 'runs', 'geodar_state_lidar_audit.yaml'),
        ])
        audit_lidar = next(item for item in audit.pipelines if item.name == 'usgs_lidar')
        self.assertEqual(audit_lidar.source.mode, 'audit')
        self.assertEqual(audit_lidar.postprocess, [])

    def test_raster_align_input_products_are_explicit_non_empty_whitelists(self):
        all_rasters = RasterAlignToTargetPostprocessor('all', input_products=None)
        selected = RasterAlignToTargetPostprocessor('terrain', input_products=['dsm', 'dtm'])

        self.assertIsNone(all_rasters.input_products)
        self.assertEqual(selected.input_products, {'dsm', 'dtm'})
        with self.assertRaisesRegex(ValueError, 'input_products must be null or a non-empty list'):
            RasterAlignToTargetPostprocessor('empty', input_products=[])
        with self.assertRaisesRegex(ValueError, 'input_products must be null or a non-empty list'):
            RasterAlignToTargetPostprocessor('blank', input_products=[''])
        with self.assertRaisesRegex(ValueError, 'cannot combine multiple products'):
            selected.validate_asset_flow(frozenset({
                AssetSpec(AssetKind.RASTER, ProductType.DSM),
                AssetSpec(AssetKind.RASTER, ProductType.DTM),
            }))

    def test_asset_contract_constants_and_model_validation(self):
        spec = AssetSpec(AssetKind.RASTER, ProductType.DEM)
        request = DownloadRequest('0', 'dem', 'https://example.test/dem.tif', 'dem.tif', 'raster', 'dem')

        self.assertEqual(spec, AssetSpec('raster', 'dem'))
        self.assertIs(request.kind, AssetKind.RASTER)
        self.assertEqual(request.product, ProductType.DEM)
        self.assertEqual(request.target_region_ids, ('0',))
        self.assertEqual(AssetStatus.SUCCESS, 'success')
        self.assertEqual(AssetStatus.REUSED, 'reused')
        with self.assertRaisesRegex(ValueError, "is not a valid AssetKind"):
            DownloadRequest('0', 'bad', 'https://example.test/bad', 'bad.bin', 'video', 'custom')
        with self.assertRaisesRegex(ValueError, 'product must be a non-empty string'):
            DownloadRequest('0', 'bad', 'https://example.test/bad', 'bad.bin', 'file', '')
        with self.assertRaisesRegex(ValueError, 'output_products must be a non-empty list'):
            USGSLidarSource(output_products=[])

    def test_config_rejects_raster_product_not_produced_by_source(self):
        default_path = os.path.join(self.project_root, 'configs', 'default.yaml')
        with self.assertRaisesRegex(ValueError, 'RasterAlign input_products'):
            load_config(
                [default_path],
                ['pipelines.usgs_lidar.source.init_args.output_products=[laz]'],
            )
        with self.assertRaisesRegex(ValueError, 'RasterAlign input_products'):
            load_config(
                [default_path],
                ['pipelines.google.source.init_args.output_format=image'],
            )

    def test_override_rejects_unknown_paths(self):
        raw = {'pipelines': [{'name': 'google', 'enable': False}]}
        with self.assertRaisesRegex(ValueError, 'Unknown override path'):
            apply_overrides(raw, ['pipelines.google.enabel=true'])

    def test_windowed_raster_mosaic_aligns_exactly_to_target(self):
        root = os.path.join(self.temp_root, 'aligned_mosaic')
        region_dir = os.path.join(root, '0')
        os.makedirs(region_dir, exist_ok=True)
        target = os.path.join(root, '0.tif')
        target_transform = from_origin(0.0, 4.0, 1.0, 1.0)
        self._write_raster(target, np.zeros((4, 4), dtype=np.float32), 'EPSG:4326', target_transform)

        left = os.path.join(region_dir, 'left.tif')
        right = os.path.join(region_dir, 'right.tif')
        self._write_raster(left, np.ones((4, 2), dtype=np.float32), 'EPSG:4326', target_transform)
        self._write_raster(
            right,
            np.full((4, 2), 2.0, dtype=np.float32),
            'EPSG:4326',
            from_origin(2.0, 4.0, 1.0, 1.0),
        )

        region = read_raster_region(target, '0')
        report = AcquisitionReport.for_regions(['0'])
        for name, path in [('left', left), ('right', right)]:
            report.add_asset(LocalAsset('0', name, path, 'raster', 'dem'), 'success')
        processor = RasterAlignToTargetPostprocessor(
            output_suffix='cop',
            input_products=['dem'],
            resampling='nearest',
            window_size=2,
        )
        processor.process(
            {'0': region},
            report,
            RuntimeContext(self.project_root, HTTPDownloadService()),
        )

        destination = os.path.join(region_dir, '0_cop.tif')
        with rasterio.open(destination) as dataset:
            self.assertEqual(dataset.shape, (4, 4))
            self.assertEqual(dataset.crs.to_epsg(), 4326)
            self.assertTrue(dataset.transform.almost_equals(target_transform))
            expected = np.column_stack((np.ones((4, 2)), np.full((4, 2), 2.0)))
            np.testing.assert_array_equal(dataset.read(1), expected)
        self.assertEqual(report.regions['0'].success[-1].product, 'dem')

    def test_postprocess_writes_directly_to_explicit_output_dir(self):
        root = os.path.join(self.temp_root, 'explicit_postprocess_output')
        target_dir = os.path.join(root, 'GT', 'AL')
        staging_dir = os.path.join(root, 'staging', 'AL__0')
        result_dir = os.path.join(root, 'results')
        os.makedirs(target_dir, exist_ok=True)
        os.makedirs(staging_dir, exist_ok=True)
        target = os.path.join(target_dir, '0.tif')
        source = os.path.join(staging_dir, 'source.tif')
        transform = from_origin(-88.0, 31.0, 0.1, 0.1)
        array = np.ones((3, 4), dtype=np.float32)
        self._write_raster(target, np.zeros_like(array), 'EPSG:4326', transform)
        self._write_raster(source, array, 'EPSG:4326', transform)

        region = read_raster_region(target, 'AL__0')
        report = AcquisitionReport.for_regions(['AL__0'])
        report.add_asset(LocalAsset('AL__0', 'source', source, 'raster', 'dsm'), 'success')
        processor = RasterAlignToTargetPostprocessor(
            output_suffix='lidar',
            input_products=['dsm'],
            output_dir=result_dir,
            resampling='nearest',
            window_size=2,
        )
        processor.process(
            {'AL__0': region},
            report,
            RuntimeContext(self.project_root, HTTPDownloadService()),
        )

        destination = os.path.join(result_dir, 'AL__0_lidar.tif')
        self.assertTrue(os.path.isfile(destination))
        self.assertFalse(os.path.exists(os.path.join(staging_dir, 'AL__0_lidar.tif')))
        with rasterio.open(destination) as dataset:
            self.assertEqual(dataset.shape, (3, 4))
            self.assertTrue(dataset.transform.almost_equals(transform))
            np.testing.assert_array_equal(dataset.read(1), array)

    def test_target_scale_uses_exact_coarse_grid(self):
        root = os.path.join(self.temp_root, 'target_scale')
        region_dir = os.path.join(root, '0')
        os.makedirs(region_dir, exist_ok=True)
        target = os.path.join(root, '0.tif')
        source = os.path.join(region_dir, 'source.tif')
        transform = from_origin(0.0, 4.0, 1.0, 1.0)
        array = np.arange(16, dtype=np.float32).reshape(4, 4)
        self._write_raster(target, np.zeros_like(array), 'EPSG:4326', transform)
        self._write_raster(source, array, 'EPSG:4326', transform)

        region = read_raster_region(target, '0')
        report = AcquisitionReport.for_regions(['0'])
        report.add_asset(LocalAsset('0', 'source', source, 'raster', 'dem'), 'success')
        processor = RasterAlignToTargetPostprocessor(
            output_suffix='cop_x2',
            input_products=['dem'],
            target_scale=2,
            resampling='average',
            window_size=2,
        )
        processor.process(
            {'0': region},
            report,
            RuntimeContext(self.project_root, HTTPDownloadService()),
        )

        with rasterio.open(os.path.join(region_dir, '0_cop_x2.tif')) as dataset:
            self.assertEqual(dataset.shape, (2, 2))
            self.assertTrue(dataset.transform.almost_equals(transform * transform.scale(2, 2)))
            np.testing.assert_allclose(dataset.read(1), [[2.5, 4.5], [10.5, 12.5]])

    def test_vertical_datum_warning_is_emitted_once_and_can_be_ignored(self):
        root = os.path.join(self.temp_root, 'vertical_warning')
        os.makedirs(root, exist_ok=True)
        target = os.path.join(root, 'target.tif')
        source_a = os.path.join(root, 'source_a.tif')
        source_b = os.path.join(root, 'source_b.tif')
        transform = from_origin(-75.0, 40.0, 0.001, 0.001)
        array = np.zeros((2, 2), dtype=np.float32)
        self._write_raster(target, array, 'EPSG:9518', transform)
        self._write_raster(source_a, array, 'EPSG:6349', transform)
        self._write_raster(source_b, array, 'EPSG:6349', transform)
        region = read_raster_region(target, 'test')
        assets = [
            LocalAsset('test', 'a', source_a, 'raster', 'dsm'),
            LocalAsset('test', 'b', source_b, 'raster', 'dsm'),
        ]

        processor = RasterAlignToTargetPostprocessor(
            output_suffix='lidar',
            input_products=['dsm'],
            vertical_datum_policy='warn_once',
        )
        output = StringIO()
        with redirect_stdout(output):
            processor._warn_vertical_datum_once({'test': region}, {'test': assets})
            processor._warn_vertical_datum_once({'test': region}, {'test': assets})
        self.assertEqual(output.getvalue().count('[postprocess/vertical]'), 1)
        self.assertIn('conversion disabled', output.getvalue())

        ignored = RasterAlignToTargetPostprocessor(
            output_suffix='lidar_ignored',
            vertical_datum_policy='ignore',
        )
        output = StringIO()
        with redirect_stdout(output):
            ignored._warn_vertical_datum_once({'test': region}, {'test': assets})
        self.assertNotIn('[postprocess/vertical]', output.getvalue())

    def test_alignment_preserves_source_vertical_crs_without_conversion(self):
        root = os.path.join(self.temp_root, 'vertical_preservation')
        region_dir = os.path.join(root, '0')
        os.makedirs(region_dir, exist_ok=True)
        target = os.path.join(root, '0.tif')
        source = os.path.join(region_dir, 'source.tif')
        transform = from_origin(-75.6, 39.6, 0.001, 0.001)
        array = np.ones((2, 2), dtype=np.float32)
        self._write_raster(target, array, 'EPSG:9518', transform)
        self._write_raster(source, array, 'EPSG:6349', transform)

        region = read_raster_region(target, '0')
        report = AcquisitionReport.for_regions(['0'])
        report.add_asset(LocalAsset('0', 'source', source, 'raster', 'dsm'), 'success')
        processor = RasterAlignToTargetPostprocessor(
            output_suffix='lidar',
            input_products=['dsm'],
            resampling='nearest',
            vertical_datum_policy='warn_once',
        )
        processor.process(
            {'0': region},
            report,
            RuntimeContext(self.project_root, HTTPDownloadService()),
        )

        with rasterio.open(os.path.join(region_dir, '0_lidar.tif')) as dataset:
            output_crs = CRS.from_wkt(dataset.crs.to_wkt())
            self.assertEqual(output_crs.sub_crs_list[0].to_epsg(), 4326)
            self.assertEqual(output_crs.sub_crs_list[1].to_epsg(), 5703)
            self.assertNotEqual(output_crs.sub_crs_list[1].to_epsg(), 3855)
            self.assertEqual(dataset.tags()['GEOACQUIRE_VERTICAL_CONVERSION'], 'false')

    @staticmethod
    def _write_target(path: str, left: float) -> None:
        with rasterio.open(
            path,
            'w',
            driver='GTiff',
            width=4,
            height=3,
            count=1,
            dtype='float32',
            crs='EPSG:4326',
            transform=from_origin(left, 30.0, 0.1, 0.1),
        ) as dataset:
            dataset.write(np.zeros((3, 4), dtype=np.float32), 1)

    @staticmethod
    def _write_raster(path: str, array: np.ndarray, crs: str, transform) -> None:
        with rasterio.open(
            path,
            'w',
            driver='GTiff',
            width=array.shape[1],
            height=array.shape[0],
            count=1,
            dtype=array.dtype,
            crs=crs,
            transform=transform,
        ) as dataset:
            dataset.write(array, 1)


if __name__ == '__main__':
    unittest.main()
