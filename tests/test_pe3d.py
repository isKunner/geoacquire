#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: test_pe3d.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: Offline PE3D naming, protocol parsing, and archive safety tests.

import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import geopandas as gpd
import requests
from shapely.geometry import box

from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import AcquireOptions, Region
from geoacquire.services.http_download import HTTPDownloadService
from geoacquire.sources.pe3d.catalog import PE3DQuadrangle, PE3DQuadrangleCatalog
from geoacquire.sources.pe3d.client import PE3DClient, PE3DDownload, PE3DProtocolError
from geoacquire.sources.pe3d.planner import plan_region_sheets, sheet_for_point
from geoacquire.sources.pe3d.products import get_product_spec
from geoacquire.sources.pe3d.source import PE3DSource
from geoacquire.sources.pe3d.tls import resolve_tls_verification
from geoacquire.regions.vector import VectorRegionProvider


class _Response:
    def __init__(self, text: str = '', content: bytes = b'', status_code: int = 200):
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'status {self.status_code}')


class _Session:
    def __init__(self, post_response: _Response | None = None):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set('PHPSESSID', 'offline-session')
        self.post_response = post_response or _Response('ok')
        self.posts: list[dict] = []

    def get(self, url, **kwargs):
        if url.endswith('get_captcha.php'):
            return _Response(content=b'captcha-image')
        return _Response('portal')

    def post(self, url, **kwargs):
        self.posts.append({'url': url, **kwargs})
        return self.post_response


class PE3DTests(unittest.TestCase):
    def test_default_tls_chain_is_packaged_and_shared_with_catalog(self):
        source = PE3DSource('offline-user', 'offline-password')
        verification = resolve_tls_verification(True)
        self.assertIsInstance(verification, str)
        self.assertTrue(os.path.isfile(verification))
        self.assertEqual(source.client.verify_tls, verification)
        self.assertEqual(source.catalog.verify_tls, verification)
        with open(verification, 'r', encoding='ascii') as stream:
            self.assertEqual(stream.read().count('BEGIN CERTIFICATE'), 2)

    def test_tls_verification_can_use_an_explicit_bundle_or_be_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = os.path.join(root, 'custom.pem')
            with open(bundle, 'w', encoding='ascii') as stream:
                stream.write('test bundle')
            self.assertEqual(resolve_tls_verification(True, bundle), os.path.abspath(bundle))
            self.assertFalse(resolve_tls_verification(False, bundle))

    def test_downloaded_sample_centres_match_their_sheet_names(self):
        samples = {
            'SC-24-X-B-IV-1-NE-D-I': (-37.2968741, -8.5520857),
            'SC-24-X-B-IV-1-NE-D-III': (-37.2968718, -8.5729191),
            'SC-25-V-A-II-4-NO-C-IV': (-35.2031259, -8.3229169),
        }
        for expected, point in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(sheet_for_point(*point).code, expected)

    def test_region_inside_sample_sheet_plans_one_quadrangle(self):
        region = Region('sample', (-37.300, -8.556, -37.290, -8.548))
        sheets = plan_region_sheets(region)
        self.assertEqual([sheet.code for sheet in sheets], ['SC-24-X-B-IV-1-NE-D-I'])
        self.assertEqual(
            sheets[0].bounds_wgs84,
            (-37.3125, -8.5625, -37.28125, -8.541666666666666),
        )

    def test_vector_provider_rejects_bad_prj_and_override_preserves_geometry(self):
        frame = gpd.GeoDataFrame(
            {'name': ['sample']},
            geometry=[box(685672.0, 9053043.0, 689224.0, 9055463.0)],
            crs='EPSG:4326',
        )
        with (
            patch('geoacquire.regions.vector.os.path.isfile', return_value=True),
            patch('geoacquire.regions.vector.gpd.read_file', return_value=frame),
        ):
            with self.assertRaisesRegex(ValueError, r'\.prj is likely incorrect'):
                VectorRegionProvider('sample.shp', id_field='name').get_regions()
            regions = VectorRegionProvider(
                'sample.shp',
                id_field='name',
                crs_override='EPSG:31984',
            ).get_regions()
        region = regions['sample']
        self.assertEqual(region.crs, 'EPSG:31984')
        self.assertIsNotNone(region.geometry_wkb_hex)

    def test_true_polygon_filter_ignores_small_tile_edge_overlaps(self):
        geometry = box(685672.9567, 9053042.9677, 689223.9567, 9055462.9677)
        region = Region(
            'sample',
            tuple(geometry.bounds),
            'EPSG:31984',
            {'geometry_wkb_hex': geometry.wkb_hex},
        )
        sheets = plan_region_sheets(region, min_intersection_fraction=0.05)
        self.assertEqual([sheet.code for sheet in sheets], ['SC-24-X-B-IV-1-NE-D-I'])

    def test_many_regions_login_once_and_batch_unique_sheets_globally(self):
        first = Region('first', (-37.300, -8.556, -37.290, -8.548))
        duplicate = Region('duplicate', (-37.299, -8.555, -37.291, -8.549))
        second = Region('second', (-37.300, -8.578, -37.290, -8.568))
        regions = {'first': first, 'duplicate': duplicate, 'second': second}
        source = PE3DSource('offline-user', 'offline-password')
        planned = {sheet.code: sheet for region in regions.values() for sheet in plan_region_sheets(region)}
        catalogue = {
            code: PE3DQuadrangle(code, str(index), 'TEST', 'green', sheet.bounds_wgs84)
            for index, (code, sheet) in enumerate(planned.items(), start=1)
        }
        source.catalog.by_sheet = lambda: catalogue
        auth_calls = []
        batches = []
        source.client.authenticate = lambda: auth_calls.append(True)

        def list_downloads(quadrangles, product):
            items = tuple(quadrangles)
            batches.append(tuple(item.sheet_code for item in items))
            return tuple(
                PE3DDownload(item.sheet_code, f'https://pe3d.pe.gov.br/{item.sheet_code}.zip', f'MDT-{item.sheet_code}.zip')
                for item in items
            )

        source.client.list_downloads = list_downloads
        source.client.download_headers = lambda: {}
        progress = AcquisitionProgress(regions)
        requests_found = list(source._request_stream(
            regions,
            progress,
            '.',
            frozenset(),
            AcquireOptions(),
        ))
        self.assertEqual(len(auth_calls), 1)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(requests_found), 2)
        first_request = next(
            request for request in requests_found if request.metadata['sheet_code'].endswith('-D-I')
        )
        self.assertEqual(first_request.target_region_ids, ('first', 'duplicate'))

    def test_link_discovery_accepts_only_requested_one_to_five_thousand_mdt(self):
        sheet = 'SC-24-X-B-IV-1-NE-D-I'
        html = (
            "<iframe src='/arquivos/1_5000/BLOCO-II/4_MDT_RASTER/MDT-"
            f"{sheet}.zip'></iframe>"
            "<iframe src='/arquivos/1_1000/BLOCO-II/4_MDT_RASTER/MDT-other.zip'></iframe>"
        )
        session = _Session(_Response(html))
        client = PE3DClient('user', 'password', session=session)
        client.authenticated = True
        quadrangle = PE3DQuadrangle(
            sheet,
            '5137',
            'TUPANATINGA',
            'green',
            (-37.3125, -8.5625, -37.28125, -8.541666666666666),
        )
        downloads = client.list_downloads([quadrangle], get_product_spec('dtm_raster'))
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0].sheet_code, sheet)
        self.assertEqual(downloads[0].filename, f'MDT-{sheet}.zip')
        payload = session.posts[0]['data']
        self.assertIn(('tipo', '4'), payload)
        self.assertIn(('id[]', '5137 - TUPANATINGA'), payload)
        self.assertIn(('mun_quad', 'quad'), payload)

    def test_authentication_writes_captcha_and_does_not_persist_answer(self):
        with tempfile.TemporaryDirectory() as root:
            captcha_path = os.path.join(root, 'captcha.png')
            session = _Session(_Response('ok'))
            client = PE3DClient('user', 'password', captcha_path=captcha_path, session=session)
            observed = []
            client.authenticate(lambda path: observed.append(path) or 'AB12')
            self.assertTrue(client.authenticated)
            self.assertFalse(os.path.exists(captcha_path))
            self.assertEqual(observed, [os.path.abspath(captcha_path)])
            self.assertEqual(session.posts[0]['data']['captcha'], 'AB12')

    def test_archive_extraction_publishes_tif_and_tfw_only(self):
        sheet = 'SC-24-X-B-IV-1-NE-D-I'
        stem = f'MDT-{sheet}'
        with tempfile.TemporaryDirectory() as root:
            archive_path = os.path.join(root, stem + '.zip')
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr(stem + '.tfw', 'world-file')
                archive.writestr(stem + '.tif', b'raster')
                archive.writestr('../../outside.txt', b'unsafe')
            destination = PE3DClient.extract_archive(
                archive_path,
                sheet,
                root,
                get_product_spec('dtm_raster'),
            )
            self.assertEqual(destination, os.path.join(root, stem + '.tif'))
            with open(destination, 'rb') as stream:
                self.assertEqual(stream.read(), b'raster')
            self.assertTrue(os.path.isfile(os.path.join(root, stem + '.tfw')))
            self.assertFalse(os.path.exists(os.path.join(root, 'outside.txt')))

    def test_existing_extracted_tile_needs_no_login(self):
        sheet = 'SC-24-X-B-IV-1-NE-D-I'
        with tempfile.TemporaryDirectory() as root:
            raster_path = os.path.join(root, f'MDT-{sheet}.tif')
            with open(raster_path, 'wb') as stream:
                stream.write(b'existing-raster')
            source = PE3DSource('offline-user', 'offline-password')
            source.client.authenticate = lambda: self.fail('local reuse must not authenticate')
            report = source.acquire(
                {'sample': Region('sample', (-37.300, -8.556, -37.290, -8.548))},
                RuntimeContext(root, HTTPDownloadService()),
                AcquireOptions(output_dir=root),
            )
            self.assertEqual(report.summary()['skipped'], 1)
            self.assertEqual(report.regions['sample'].skipped[0].path, raster_path)
            self.assertEqual(report.regions['sample'].skipped[0].product, 'dtm')

    def test_sheet_missing_from_public_catalog_needs_no_login(self):
        with tempfile.TemporaryDirectory() as root:
            source = PE3DSource('offline-user', 'offline-password')
            source.catalog.by_sheet = lambda: {}
            source.client.authenticate = lambda: self.fail('missing sheet must not authenticate')
            source.client.download_headers = lambda: self.fail('missing sheet has no cookies')
            report = source.acquire(
                {'sample': Region('sample', (-37.300, -8.556, -37.290, -8.548))},
                RuntimeContext(root, HTTPDownloadService()),
                AcquireOptions(output_dir=root),
            )
            self.assertEqual(report.summary()['failed'], 1)
            self.assertIn('no available entry', report.regions['sample'].failed[0].error)

    def test_untrusted_iframe_host_is_rejected(self):
        sheet = 'SC-24-X-B-IV-1-NE-D-I'
        session = _Session(_Response(
            f"<iframe src='https://example.invalid/1_5000/4_MDT_RASTER/MDT-{sheet}.zip'></iframe>"
        ))
        client = PE3DClient('user', 'password', session=session)
        client.authenticated = True
        quadrangle = PE3DQuadrangle(
            sheet,
            '5137',
            'TUPANATINGA',
            'green',
            (-37.3125, -8.5625, -37.28125, -8.541666666666666),
        )
        with self.assertRaises(PE3DProtocolError):
            client.list_downloads([quadrangle], get_product_spec('dtm_raster'))

    def test_catalog_converts_portal_id_and_duplicate_municipalities_to_one_sheet(self):
        ring = [
            [-37.3125, -8.5625],
            [-37.3125, -8.541666666666666],
            [-37.28125, -8.541666666666666],
            [-37.28125, -8.5625],
            [-37.3125, -8.5625],
        ]
        data = {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'properties': {'id_quad': '5137', 'name': 'TUPANATINGA', 'color': 'green'},
                    'geometry': {'type': 'Polygon', 'coordinates': [ring]},
                },
                {
                    'type': 'Feature',
                    'properties': {'id_quad': '5137', 'name': 'BUÍQUE', 'color': 'green'},
                    'geometry': {'type': 'Polygon', 'coordinates': [ring]},
                },
            ],
        }
        catalogue = PE3DQuadrangleCatalog.parse(data)
        self.assertEqual(list(catalogue), ['SC-24-X-B-IV-1-NE-D-I'])
        self.assertEqual(
            catalogue['SC-24-X-B-IV-1-NE-D-I'].selection_value,
            '5137 - TUPANATINGA',
        )

    def test_other_portal_products_are_reserved_but_disabled(self):
        with self.assertRaisesRegex(ValueError, 'reserved for future support'):
            get_product_spec('dsm_raster')


if __name__ == '__main__':
    unittest.main()
