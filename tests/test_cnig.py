#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Offline CNIG portal API parsing and polygon-planning tests."""

import tempfile
import unittest
from unittest.mock import Mock, patch

import geopandas as gpd
from shapely.geometry import Point, Polygon, box, mapping

from geoacquire.core.models import Region, RegionMetadataKey
from geoacquire.regions.point import PointRegionProvider
from geoacquire.sources.cnig.client import CNIGTile, _CNIGFileTableParser, _result_total
from geoacquire.sources.cnig.source import CNIGMDTSource


class CNIGSourceTests(unittest.TestCase):
    @staticmethod
    def _tile(sequential: int, filename: str, geometry) -> CNIGTile:
        return CNIGTile(str(sequential), filename, mapping(geometry))

    def test_portal_html_parser_pairs_native_tif_with_sequential(self):
        fragment = '''
            <input type="hidden" id="totalArchivos" value="2">
            <table><tbody>
              <tr><td>MDT50CM-ETRS89-H29-0984-5-6-COB3-V1.tif</td>
                  <td><img id="target_12658243"></td></tr>
              <tr><td>MDT50CM-ETRS89-H30-0984-5-6-COB3-V1.tif</td>
                  <td><a id="linkDescDir_12653952"></a></td></tr>
            </tbody></table>
        '''
        parser = _CNIGFileTableParser('MDT50CM-')
        parser.feed(fragment)

        self.assertEqual(_result_total(fragment), 2)
        self.assertEqual(parser.results, [
            ('12658243', 'MDT50CM-ETRS89-H29-0984-5-6-COB3-V1.tif'),
            ('12653952', 'MDT50CM-ETRS89-H30-0984-5-6-COB3-V1.tif'),
        ])

    def test_source_uses_true_polygon_and_prefers_the_local_utm_zone(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = CNIGMDTSource(footprint_cache_path=temporary + '/footprints.json')
            shared = box(-5.99, 37.39, -5.98, 37.40)
            bbox_only = box(-5.91, 37.48, -5.90, 37.49)
            source.client.query_tiles = Mock(return_value=(
                self._tile(
                    1,
                    'MDT50CM-ETRS89-H29-0984-5-6-COB3-V1.tif',
                    shared,
                ),
                self._tile(
                    2,
                    'MDT50CM-ETRS89-H30-0984-5-6-COB3-V1.tif',
                    shared,
                ),
                self._tile(
                    3,
                    'MDT50CM-ETRS89-H30-0984-8-8-COB3-V1.tif',
                    bbox_only,
                ),
            ))
            triangle = Polygon([(-6.0, 37.38), (-5.88, 37.38), (-6.0, 37.50)])
            regions = {
                'triangle': Region(
                    'triangle',
                    triangle.bounds,
                    metadata={RegionMetadataKey.GEOMETRY_WKB_HEX: triangle.wkb_hex},
                ),
                'small': Region('small', (-5.988, 37.392, -5.982, 37.398)),
            }

            requests = list(source.build_requests(regions))

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(
            request.filename,
            'MDT50CM-ETRS89-H30-0984-5-6-COB3-V1.tif',
        )
        self.assertEqual(request.target_region_ids, ('triangle', 'small'))
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.data, {'secDescDirLA': '2'})
        self.assertEqual(request.metadata['source_crs'], 'EPSG:25830')
        self.assertEqual(request.product, 'dtm')

    def test_source_refuses_more_than_the_official_anonymous_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = CNIGMDTSource(footprint_cache_path=temporary + '/footprints.json')
            footprint = box(-3.8, 40.3, -3.6, 40.5)
            source.client.query_tiles = Mock(return_value=tuple(
                self._tile(
                    index,
                    f'MDT50CM-ETRS89-H30-0559-{index}-1-COB3-V1.tif',
                    footprint,
                )
                for index in range(1, 22)
            ))

            with self.assertRaisesRegex(RuntimeError, 'official anonymous limit is 20'):
                list(source.build_requests({'madrid': Region('madrid', footprint.bounds)}))

    def test_source_accepts_metric_query_region_from_point_provider(self):
        frame = gpd.GeoDataFrame(
            {'ID': ['seville_demo']},
            geometry=[Point(-5.9845, 37.3891)],
            crs='EPSG:4326',
        )
        with (
            patch('geoacquire.regions.point.os.path.isfile', return_value=True),
            patch('geoacquire.regions.point.gpd.read_file', return_value=frame),
        ):
            regions = PointRegionProvider(
                'spain_points.shp', id_field='ID', query_size_m=500.0,
            ).get_regions()
        with tempfile.TemporaryDirectory() as temporary:
            source = CNIGMDTSource(footprint_cache_path=temporary + '/footprints.json')
            source.client.query_tiles = Mock(return_value=(
                self._tile(
                    1,
                    'MDT50CM-ETRS89-H30-0984-5-6-COB3-V1.tif',
                    box(-6.0, 37.37, -5.97, 37.41),
                ),
            ))

            requests = list(source.build_requests(regions))

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].target_region_ids, ('seville_demo',))
        self.assertEqual(requests[0].product, 'dtm')
        self.assertEqual(regions['seville_demo'].metadata['query_size_m'], 500.0)


if __name__ == '__main__':
    unittest.main()
