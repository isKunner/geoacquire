#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: test_linz.py
# @Time    : 2026/9/1
# @Author  : Kevin
# @Describe: Offline LINZ static-STAC parsing, cache, and DEM tile-selection tests.

import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import geopandas as gpd
from shapely.geometry import Point, Polygon

from geoacquire.core.geo import transform_bounds
from geoacquire.core.models import Region, RegionMetadataKey
from geoacquire.regions.point import PointRegionProvider
from geoacquire.sources.linz.catalog import LINZSTACTile, LINZStaticSTACCatalog
from geoacquire.sources.linz.dem_1m import LINZDEM1mSource


class LINZSourceTests(unittest.TestCase):
    COLLECTION_URL = 'https://example.test/nz/collection.json'

    @staticmethod
    def _tile(
        tile_id: str,
        bounds: tuple[float, float, float, float],
        geometry: dict | None = None,
    ) -> LINZSTACTile:
        left, bottom, right, top = bounds
        geometry = geometry or {
            'type': 'Polygon',
            'coordinates': [[
                [left, bottom], [right, bottom], [right, top],
                [left, top], [left, bottom],
            ]],
        }
        return LINZSTACTile(
            tile_id=tile_id,
            bounds_wgs84=bounds,
            geometry=geometry,
            url=f'https://example.test/nz/{tile_id}.tiff',
            item_url=f'https://example.test/nz/{tile_id}.json',
            start_datetime='2020-01-01T00:00:00Z',
            end_datetime='2020-02-01T00:00:00Z',
            checksum=f'checksum-{tile_id}',
        )

    def test_source_selects_intersecting_cog_and_preserves_linz_metadata(self):
        source = LINZDEM1mSource(collection_url=self.COLLECTION_URL)
        source.catalog.list_tiles = Mock(return_value=(
            self._tile('BQ31', (174.70, -41.40, 174.90, -41.20)),
            self._tile('BP30', (173.00, -40.00, 173.20, -39.80)),
        ))
        region = Region('wellington', (174.775, -41.295, 174.785, -41.285))

        requests = list(source.build_requests({'wellington': region}))

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.asset_id, 'linz:nz_dem_1m:BQ31')
        self.assertEqual(request.url, 'https://example.test/nz/BQ31.tiff')
        self.assertEqual(request.filename, 'BQ31_nz_dem_1m.tiff')
        self.assertEqual(request.kind, 'raster')
        self.assertEqual(request.product, 'dem')
        self.assertEqual(request.metadata['source_crs'], 'EPSG:2193')
        self.assertEqual(request.metadata['vertical_crs'], 'EPSG:7839')
        self.assertEqual(request.metadata['vertical_datum'], 'NZVD2016')
        self.assertEqual(request.metadata['capture_start'], '2020-01-01T00:00:00Z')

    def test_source_transforms_projected_region_and_checks_exact_footprint(self):
        outside_triangle = {
            'type': 'Polygon',
            'coordinates': [[
                [174.70, -41.40], [174.90, -41.40], [174.70, -41.20], [174.70, -41.40],
            ]],
        }
        selected = self._tile('BQ31', (174.70, -41.40, 174.90, -41.20))
        bbox_only_false_positive = self._tile(
            'BQ32', (174.70, -41.40, 174.90, -41.20), outside_triangle,
        )
        source = LINZDEM1mSource(collection_url=self.COLLECTION_URL, filename_suffix=None)
        source.catalog.list_tiles = Mock(return_value=(selected, bbox_only_false_positive))
        projected = transform_bounds(
            (174.84, -41.24, 174.85, -41.23), 'EPSG:4326', 'EPSG:2193',
        )
        region = Region('projected', projected, 'EPSG:2193')

        requests = list(source.build_requests({'projected': region}))

        self.assertEqual([request.filename for request in requests], ['BQ31.tiff'])

    def test_source_uses_true_vector_polygon_instead_of_its_bounding_box(self):
        source = LINZDEM1mSource(collection_url=self.COLLECTION_URL, filename_suffix=None)
        source.catalog.list_tiles = Mock(return_value=(
            self._tile('inside', (0.10, 0.10, 0.20, 0.20)),
            self._tile('bbox-only', (1.70, 1.70, 1.80, 1.80)),
        ))
        triangle = Polygon([(0.0, 0.0), (2.0, 0.0), (0.0, 2.0)])
        region = Region(
            'triangle',
            triangle.bounds,
            metadata={RegionMetadataKey.GEOMETRY_WKB_HEX: triangle.wkb_hex},
        )

        requests = list(source.build_requests({'triangle': region}))

        self.assertEqual([request.filename for request in requests], ['inside.tiff'])

    def test_source_plans_one_shared_cog_for_multiple_vector_features(self):
        source = LINZDEM1mSource(collection_url=self.COLLECTION_URL, filename_suffix=None)
        source.catalog.list_tiles = Mock(return_value=(
            self._tile('shared', (174.70, -41.40, 174.90, -41.20)),
        ))
        regions = {
            'first': Region('first', (174.75, -41.35, 174.80, -41.30)),
            'second': Region('second', (174.80, -41.30, 174.85, -41.25)),
        }

        requests = list(source.build_requests(regions))

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].target_region_ids, ('first', 'second'))

    def test_source_accepts_metric_query_region_from_point_provider(self):
        frame = gpd.GeoDataFrame(
            {'ID': ['wellington_demo']},
            geometry=[Point(174.78, -41.29)],
            crs='EPSG:4326',
        )
        with (
            patch('geoacquire.regions.point.os.path.isfile', return_value=True),
            patch('geoacquire.regions.point.gpd.read_file', return_value=frame),
        ):
            regions = PointRegionProvider(
                'nz_points.shp', id_field='ID', query_size_m=500.0,
            ).get_regions()
        source = LINZDEM1mSource(collection_url=self.COLLECTION_URL, filename_suffix=None)
        source.catalog.list_tiles = Mock(return_value=(
            self._tile('BQ31', (174.70, -41.40, 174.90, -41.20)),
        ))

        requests = list(source.build_requests(regions))

        self.assertEqual([request.filename for request in requests], ['BQ31.tiff'])
        self.assertEqual(requests[0].target_region_ids, ('wellington_demo',))
        self.assertEqual(regions['wellington_demo'].metadata['query_size_m'], 500.0)

    def test_static_stac_refresh_resolves_links_and_reuses_disk_cache(self):
        collection = {
            'type': 'Collection',
            'links': [
                {'rel': 'self', 'href': './collection.json'},
                {'rel': 'item', 'href': './BQ31.json'},
            ],
        }
        item = {
            'type': 'Feature',
            'id': 'BQ31',
            'bbox': [174.70, -41.40, 174.90, -41.20],
            'geometry': self._tile('BQ31', (174.70, -41.40, 174.90, -41.20)).geometry,
            'properties': {
                'start_datetime': '2019-01-01T00:00:00Z',
                'end_datetime': '2020-01-01T00:00:00Z',
            },
            'assets': {
                'visual': {
                    'href': './BQ31.tiff',
                    'type': 'image/tiff; application=geotiff; profile=cloud-optimized',
                    'file:checksum': 'sha256-value',
                },
            },
        }

        def response(value):
            result = Mock()
            result.raise_for_status.return_value = None
            result.json.return_value = value
            return result

        with tempfile.TemporaryDirectory() as temporary:
            cache_path = os.path.join(temporary, 'linz-index.json')
            with patch(
                'geoacquire.sources.linz.catalog.requests.get',
                side_effect=[response(collection), response(item)],
            ) as get:
                first = LINZStaticSTACCatalog(
                    self.COLLECTION_URL, cache_path=cache_path, max_workers=1,
                ).list_tiles()
            self.assertEqual(get.call_count, 2)
            self.assertEqual(first[0].url, 'https://example.test/nz/BQ31.tiff')
            self.assertEqual(first[0].checksum, 'sha256-value')

            with open(cache_path, 'r', encoding='utf-8') as file:
                cached = json.load(file)
            self.assertEqual(cached['schema_version'], 1)
            self.assertEqual(cached['tiles'][0]['tile_id'], 'BQ31')

            with patch('geoacquire.sources.linz.catalog.requests.get') as get:
                second = LINZStaticSTACCatalog(
                    self.COLLECTION_URL, cache_path=cache_path, max_workers=1,
                ).list_tiles()
            get.assert_not_called()
            self.assertEqual(second, first)

    def test_stale_cache_is_used_when_catalog_refresh_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = os.path.join(temporary, 'linz-index.json')
            seed = LINZStaticSTACCatalog(self.COLLECTION_URL, cache_path=cache_path)
            seed._write_cache((self._tile('BQ31', (174.70, -41.40, 174.90, -41.20)),))
            old = os.path.getmtime(cache_path) - 20 * 86400
            os.utime(cache_path, (old, old))

            catalog = LINZStaticSTACCatalog(
                self.COLLECTION_URL, cache_path=cache_path, cache_days=10.0,
            )
            catalog._request_json = Mock(side_effect=RuntimeError('offline'))

            tiles = catalog.list_tiles()

            self.assertEqual([tile.tile_id for tile in tiles], ['BQ31'])


if __name__ == '__main__':
    unittest.main()
