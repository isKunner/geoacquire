#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: wesm.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Download, cache, and spatially query USGS WESM workunit metadata.

import os
from collections.abc import Mapping
from typing import Any

import geopandas as gpd
import requests
from pyproj import CRS
from shapely.geometry import box

from geoacquire.core.geo import horizontal_crs, transform_bounds
from geoacquire.core.models import Region
from geoacquire.services.http_stream import is_complete_range, write_response_body

WESM_URL = 'https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/metadata/WESM.gpkg'
WESM_MIN_SIZE = 3 * 1024 * 1024 * 1024
WESM_COLUMNS = (
    'workunit',
    'project',
    'lpc_link',
    'sourcedem_link',
    'collect_end',
    'horiz_crs',
)


class WESMClient:
    """Spatial-indexed access to the large WESM GeoPackage."""

    def __init__(self, cache_dir: str = './cache/wesm'):
        self.cache_dir = cache_dir
        self.gpkg_path = os.path.join(cache_dir, 'WESM.gpkg')
        self._file_crs: CRS | None = None

    def _download(self) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        part_path = self.gpkg_path + '.part'
        # Older versions wrote incomplete bytes to the final name. Preserve
        # those bytes for resume; new downloads publish only complete files.
        if os.path.isfile(self.gpkg_path) and not os.path.isfile(part_path):
            os.replace(self.gpkg_path, part_path)
        resume_byte = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
        headers = {'Accept-Encoding': 'identity'}
        if resume_byte > 0:
            headers['Range'] = f'bytes={resume_byte}-'

        with requests.get(WESM_URL, headers=headers, stream=True, timeout=(30, 600)) as response:
            if not is_complete_range(response, resume_byte):
                response.raise_for_status()
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type:
                    raise RuntimeError(f'WESM URL returned HTML: {content_type}')
                write_response_body(response, part_path, resume_byte, 1024 * 1024)

        size = os.path.getsize(part_path)
        if size < WESM_MIN_SIZE:
            raise RuntimeError(f'WESM file is incomplete ({size / 1024 / 1024:.0f} MB), expected about 3.4 GB')
        os.replace(part_path, self.gpkg_path)

    def _ensure_downloaded(self) -> None:
        if os.path.isfile(self.gpkg_path) and os.path.getsize(self.gpkg_path) >= WESM_MIN_SIZE:
            return
        self._download()

    def _get_file_crs(self) -> CRS:
        if self._file_crs is None:
            crs = gpd.read_file(self.gpkg_path, rows=1, columns=[]).crs
            if crs is None:
                raise RuntimeError(f'WESM has no CRS: {self.gpkg_path}')
            self._file_crs = CRS.from_user_input(crs)
        return self._file_crs

    @staticmethod
    def _region_shape(region: Region, file_crs: CRS):
        """Build a WESM query envelope without requiring optional datum grids.

        WESM uses NAD83 geographic coordinates, while target rasters commonly
        use WGS 84 (sometimes inside a compound horizontal/vertical CRS).
        Their small horizontal datum difference is irrelevant when selecting
        state-scale LiDAR workunits. Treating their longitude/latitude values as
        equivalent also keeps an offline machine from requesting NOAA grids.
        """
        source = horizontal_crs(region.crs)
        target = horizontal_crs(file_crs)
        if {source.to_epsg(), target.to_epsg()} == {4326, 4269}:
            return box(*region.bounds)
        return box(*transform_bounds(region.bounds, source, target))

    def query_many(self, regions: Mapping[str, Region]) -> dict[str, list[dict[str, Any]]]:
        """Query one target-directory batch with one GeoPackage read.

        Raster-directory inputs commonly contain many small adjacent targets.
        Their envelopes are transformed once, a single spatial-index query reads
        candidate workunits, and inexpensive in-memory intersection assigns those
        workunits back to each Region.
        """
        if not regions:
            return {}

        self._ensure_downloaded()
        file_crs = self._get_file_crs()
        region_shapes = {
            region_id: self._region_shape(region, file_crs)
            for region_id, region in regions.items()
        }
        minimum_x = min(shape.bounds[0] for shape in region_shapes.values())
        minimum_y = min(shape.bounds[1] for shape in region_shapes.values())
        maximum_x = max(shape.bounds[2] for shape in region_shapes.values())
        maximum_y = max(shape.bounds[3] for shape in region_shapes.values())
        data = gpd.read_file(
            self.gpkg_path,
            bbox=(minimum_x, minimum_y, maximum_x, maximum_y),
            columns=list(WESM_COLUMNS),
        )

        missing = [column for column in WESM_COLUMNS if column not in data.columns]
        if missing:
            raise RuntimeError(f'WESM is missing required columns: {missing}')

        results: dict[str, list[dict[str, Any]]] = {}
        file_crs_text = file_crs.to_string()
        for region_id, shape in region_shapes.items():
            intersected = data[data.intersects(shape)]
            records = []
            for _, row in intersected.drop_duplicates('workunit').iterrows():
                record = {column: row[column] for column in WESM_COLUMNS}
                record['_wesm_bounds'] = tuple(float(value) for value in row.geometry.bounds)
                record['_wesm_crs'] = file_crs_text
                records.append(record)
            results[region_id] = records
        return results
