#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Point Region and native, non-resampling crop regressions."""

import os
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import rasterio
from pyproj import CRS
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import (
    AcquisitionReport,
    AssetKind,
    AssetStatus,
    LocalAsset,
    Region,
    RegionMetadataKey,
)
from geoacquire.postprocess.native_point_crop import NativePointWindowPostprocessor
from geoacquire.regions.point import PointRegionProvider


class PointRegionTests(unittest.TestCase):
    def test_geographic_point_uses_metric_query_and_attribute_filter(self):
        frame = gpd.GeoDataFrame(
            {'ID': [512989, 1], 'size': ['large', 'small']},
            geometry=[Point(-39.8976246759, -8.2293747602), Point(-40.0, -8.0)],
            crs='EPSG:4326',
        )
        with (
            patch('geoacquire.regions.point.os.path.isfile', return_value=True),
            patch('geoacquire.regions.point.gpd.read_file', return_value=frame),
        ):
            regions = PointRegionProvider(
                'points.shp',
                id_field='ID',
                query_size_m=500.0,
                include_values={'ID': ['512989']},
            ).get_regions()
        self.assertEqual(list(regions), ['512989'])
        region = regions['512989']
        self.assertEqual(region.point, (-39.8976246759, -8.2293747602))
        self.assertEqual(region.point_crs, 'EPSG:4326')
        self.assertEqual(region.metadata[RegionMetadataKey.QUERY_SIZE_M], 500.0)
        self.assertEqual(region.metadata['vector_properties']['size'], 'large')
        geod = CRS.from_epsg(4326).get_geod()
        longitude_span = geod.inv(
            region.bounds[0], region.point[1], region.bounds[2], region.point[1]
        )[2]
        latitude_span = geod.inv(
            region.point[0], region.bounds[1], region.point[0], region.bounds[3]
        )[2]
        self.assertAlmostEqual(longitude_span, 500.0, delta=0.1)
        self.assertAlmostEqual(latitude_span, 500.0, delta=0.1)

    def test_projected_point_respects_non_metre_axis_units(self):
        frame = gpd.GeoDataFrame(
            {'ID': [1]},
            geometry=[Point(1000000.0, 200000.0)],
            crs='EPSG:2263',
        )
        with (
            patch('geoacquire.regions.point.os.path.isfile', return_value=True),
            patch('geoacquire.regions.point.gpd.read_file', return_value=frame),
        ):
            region = PointRegionProvider(
                'points.shp', id_field='ID', query_size_m=500.0
            ).get_regions()['1']
        expected = 500.0 / CRS.from_epsg(2263).axis_info[0].unit_conversion_factor
        self.assertAlmostEqual(region.bounds[2] - region.bounds[0], expected, places=6)
        self.assertAlmostEqual(region.bounds[3] - region.bounds[1], expected, places=6)

    def test_point_provider_rejects_polygon_input(self):
        frame = gpd.GeoDataFrame({'ID': [1]}, geometry=[box(0, 0, 1, 1)], crs='EPSG:4326')
        with (
            patch('geoacquire.regions.point.os.path.isfile', return_value=True),
            patch('geoacquire.regions.point.gpd.read_file', return_value=frame),
        ):
            with self.assertRaisesRegex(ValueError, 'only Point'):
                PointRegionProvider('points.shp', id_field='ID').get_regions()


class NativePointWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name

    def _write_raster(self, name, data, transform):
        path = os.path.join(self.root, name)
        with rasterio.open(
            path,
            'w',
            driver='GTiff',
            width=data.shape[1],
            height=data.shape[0],
            count=1,
            dtype=data.dtype,
            crs='EPSG:31984',
            transform=transform,
            nodata=-32767.0,
        ) as target:
            target.write(data, 1)
        return path

    @staticmethod
    def _region(region_id, point):
        return Region(
            region_id,
            (point[0] - 250, point[1] - 250, point[0] + 250, point[1] + 250),
            'EPSG:31984',
            {
                RegionMetadataKey.POINT_X: point[0],
                RegionMetadataKey.POINT_Y: point[1],
                RegionMetadataKey.POINT_CRS: 'EPSG:31984',
                RegionMetadataKey.QUERY_SIZE_M: 500.0,
            },
        )

    @staticmethod
    def _report(region_id, paths):
        report = AcquisitionReport.for_regions([region_id])
        for index, path in enumerate(paths):
            report.add_asset(
                LocalAsset(region_id, f'tile-{index}', path, AssetKind.RASTER, 'dtm'),
                AssetStatus.SUCCESS,
            )
        return report

    def test_crop_copies_exact_native_pixels_and_snaps_the_center(self):
        data = np.arange(1000 * 1000, dtype=np.float32).reshape(1000, 1000)
        source = self._write_raster('source.tif', data, from_origin(1000, 2000, 1, 1))
        point = (1400.2, 1600.3)
        region = self._region('512989', point)
        processor = NativePointWindowPostprocessor(
            448.0,
            ['dtm'],
            output_dir=os.path.join(self.root, 'gt'),
            fallback_resampling='bilinear',
        )
        report = processor.process(
            {'512989': region},
            self._report('512989', [source]),
            RuntimeContext(self.root, None),
        )
        self.assertEqual(report.summary()['failed'], 0)
        output = os.path.join(self.root, 'gt', '512989.tif')
        with rasterio.open(output) as result:
            self.assertEqual((result.height, result.width), (448, 448))
            np.testing.assert_array_equal(result.read(1), data[176:624, 176:624])
            self.assertEqual(result.tags()['GEOACQUIRE_RESAMPLING'], 'none')
            self.assertLessEqual(abs(float(result.tags()['GEOACQUIRE_CENTER_OFFSET_X'])), 0.5)
            self.assertLessEqual(abs(float(result.tags()['GEOACQUIRE_CENTER_OFFSET_Y'])), 0.5)

    def test_aligned_adjacent_tiles_are_mosaicked_without_resampling(self):
        left = self._write_raster(
            'left.tif', np.ones((500, 500), dtype=np.float32), from_origin(0, 500, 1, 1)
        )
        right = self._write_raster(
            'right.tif', np.full((500, 500), 2, dtype=np.float32), from_origin(500, 500, 1, 1)
        )
        point = (500.0, 250.0)
        processor = NativePointWindowPostprocessor(
            448.0, ['dtm'], output_dir=os.path.join(self.root, 'gt')
        )
        report = processor.process(
            {'dam': self._region('dam', point)},
            self._report('dam', [left, right]),
            RuntimeContext(self.root, None),
        )
        self.assertEqual(report.summary()['failed'], 0)
        with rasterio.open(os.path.join(self.root, 'gt', 'dam.tif')) as result:
            values = result.read(1)
        self.assertTrue(np.all(values[:, :224] == 1))
        self.assertTrue(np.all(values[:, 224:] == 2))

    def test_small_resolution_mismatch_only_resamples_missing_pixels(self):
        left = self._write_raster(
            'left.tif', np.ones((500, 500), dtype=np.float32), from_origin(0, 500, 1, 1)
        )
        right = self._write_raster(
            'right.tif', np.full((500, 500), 2, dtype=np.float32),
            from_origin(500, 500, 1, 0.9998),
        )
        processor = NativePointWindowPostprocessor(
            448.0,
            ['dtm'],
            output_dir=os.path.join(self.root, 'gt'),
            fallback_resampling='bilinear',
            max_resolution_mismatch_fraction=0.001,
            max_resampled_fraction=0.25,
        )
        report = processor.process(
            {'dam': self._region('dam', (380.0, 250.0))},
            self._report('dam', [left, right]),
            RuntimeContext(self.root, None),
        )
        self.assertEqual(report.summary()['failed'], 0)
        with rasterio.open(os.path.join(self.root, 'gt', 'dam.tif')) as result:
            values = result.read(1)
            tags = result.tags()
        self.assertTrue(np.all(values[:, :344] == 1))
        self.assertTrue(np.all(values[:, 344:] == 2))
        self.assertEqual(tags['GEOACQUIRE_RESAMPLING'], 'partial_bilinear')
        self.assertEqual(int(tags['GEOACQUIRE_RESAMPLED_PIXELS']), 448 * 104)
        self.assertAlmostEqual(float(tags['GEOACQUIRE_RESAMPLED_FRACTION']), 104 / 448)
        self.assertEqual(tags['GEOACQUIRE_RESAMPLED_SOURCES'], 'right.tif')

    def test_resolution_fallback_respects_resampled_fraction_cap(self):
        left = self._write_raster(
            'left.tif', np.ones((500, 500), dtype=np.float32), from_origin(0, 500, 1, 1)
        )
        right = self._write_raster(
            'right.tif', np.full((500, 500), 2, dtype=np.float32),
            from_origin(500, 500, 1, 0.9998),
        )
        processor = NativePointWindowPostprocessor(
            448.0,
            ['dtm'],
            output_dir=os.path.join(self.root, 'gt'),
            fallback_resampling='bilinear',
            max_resampled_fraction=0.20,
        )
        report = processor.process(
            {'dam': self._region('dam', (380.0, 250.0))},
            self._report('dam', [left, right]),
            RuntimeContext(self.root, None),
        )
        self.assertEqual(report.summary()['failed'], 1)
        self.assertIn('resampled fraction', report.regions['dam'].failed[0].error)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'gt', 'dam.tif')))

    def test_resolution_fallback_respects_mismatch_threshold(self):
        left = self._write_raster(
            'left.tif', np.ones((500, 500), dtype=np.float32), from_origin(0, 500, 1, 1)
        )
        right = self._write_raster(
            'right.tif', np.full((500, 500), 2, dtype=np.float32),
            from_origin(500, 500, 1, 0.9998),
        )
        processor = NativePointWindowPostprocessor(
            448.0,
            ['dtm'],
            output_dir=os.path.join(self.root, 'gt'),
            fallback_resampling='bilinear',
            max_resolution_mismatch_fraction=0.0001,
        )
        report = processor.process(
            {'dam': self._region('dam', (380.0, 250.0))},
            self._report('dam', [left, right]),
            RuntimeContext(self.root, None),
        )
        self.assertEqual(report.summary()['failed'], 1)
        self.assertIn('resolution/rotation', report.regions['dam'].failed[0].error)

    def test_misaligned_tile_is_not_silently_resampled(self):
        left = self._write_raster(
            'left.tif', np.ones((500, 500), dtype=np.float32), from_origin(0, 500, 1, 1)
        )
        shifted = self._write_raster(
            'shifted.tif', np.ones((500, 500), dtype=np.float32), from_origin(500.25, 500, 1, 1)
        )
        processor = NativePointWindowPostprocessor(
            448.0, ['dtm'], output_dir=os.path.join(self.root, 'gt')
        )
        report = processor.process(
            {'dam': self._region('dam', (500.0, 250.0))},
            self._report('dam', [left, shifted]),
            RuntimeContext(self.root, None),
        )
        self.assertEqual(report.summary()['failed'], 1)
        self.assertIn('grid origin', report.regions['dam'].failed[0].error)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'gt', 'dam.tif')))

    def test_explicit_half_pixel_grid_snap_copies_values_without_interpolation(self):
        left = self._write_raster(
            'left.tif', np.ones((500, 500), dtype=np.float32), from_origin(0, 500, 1, 1)
        )
        shifted = self._write_raster(
            'shifted.tif', np.full((500, 500), 2, dtype=np.float32),
            from_origin(500.25, 500.4, 1, 1),
        )
        processor = NativePointWindowPostprocessor(
            448.0,
            ['dtm'],
            output_dir=os.path.join(self.root, 'gt'),
            max_grid_snap_pixels=0.5,
        )
        report = processor.process(
            {'dam': self._region('dam', (500.0, 250.0))},
            self._report('dam', [left, shifted]),
            RuntimeContext(self.root, None),
        )
        self.assertEqual(report.summary()['failed'], 0)
        with rasterio.open(os.path.join(self.root, 'gt', 'dam.tif')) as result:
            values = result.read(1)
            tags = result.tags()
        self.assertTrue(np.all(values[:, :224] == 1))
        self.assertTrue(np.all(values[:, 224:] == 2))
        self.assertEqual(tags['GEOACQUIRE_RESAMPLING'], 'none')
        self.assertAlmostEqual(float(tags['GEOACQUIRE_MAX_GRID_SNAP_PIXELS']), 0.4)


if __name__ == '__main__':
    unittest.main()
