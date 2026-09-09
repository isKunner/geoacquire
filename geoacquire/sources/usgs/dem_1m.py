#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: dem_1m.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: USGS 1-meter DEM acquisition from WESM project link lists.

import os
import re
from urllib.parse import urlparse

from geoacquire.core.geo import transform_bounds
from geoacquire.core.models import AssetKind, AssetSpec, DownloadRequest, ProductType, Region

from .base import USGSWorkunitHTTPSource

ROCKYWEB_1M = 'https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/1m/Projects'
TILE_RE = re.compile(r'USGS_1M_(\d{1,2})_x(\d+)y(\d+)', re.IGNORECASE)
TILE_GRID = 10000.0


class USGSDEM1mSource(USGSWorkunitHTTPSource):
    """Select native 1 m GeoTIFFs; no LAZ conversion or LiDAR catalogue is involved.

    The UTM name encodes a 10 km tile's north edge: y is reduced by one
    tile when computing its southern bound. hemisphere selects the UTM CRS.
    """

    OUTPUT_SPECS = frozenset({AssetSpec(AssetKind.RASTER, ProductType.DEM)})

    def __init__(
        self,
        hemisphere: str = 'north',
        filename_suffix: str | None = 'usgs_dem_1m',
        wesm_cache_dir: str = './cache/wesm',
        link_cache_dir: str = './cache/link_lists',
        link_cache_days: float = 10.0,
    ):
        super().__init__(wesm_cache_dir, link_cache_dir, link_cache_days)
        if hemisphere not in ('north', 'south'):
            raise ValueError("hemisphere must be 'north' or 'south'")
        self.hemisphere = hemisphere
        self.filename_suffix = filename_suffix

    def _parse_tile(self, url: str) -> tuple[int, float, float] | None:
        match = TILE_RE.search(os.path.basename(urlparse(url).path))
        if not match:
            return None
        zone, x, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        epsg = (32600 if self.hemisphere == 'north' else 32700) + zone
        return epsg, x * TILE_GRID, (y - 1) * TILE_GRID

    def _build_workunit_requests(
        self,
        region_id: str,
        region: Region,
        workunit: dict,
    ) -> list[tuple[tuple, DownloadRequest]]:
        project = workunit.get('project')
        if not project:
            return []
        text = self._fetch_text(f'{ROCKYWEB_1M}/{project}/0_file_download_links.txt')
        urls = [line.strip() for line in text.splitlines() if line.strip().lower().endswith(('.tif', '.tiff'))]
        bounds_by_epsg: dict[int, tuple[float, float, float, float]] = {}
        kept: list[tuple[tuple, DownloadRequest]] = []

        for url in urls:
            parsed = self._parse_tile(url)
            if parsed is None:
                print(f'[usgs/dem_1m] unrecognized tile name: {os.path.basename(urlparse(url).path)}')
                continue
            epsg, easting, northing = parsed
            if epsg not in bounds_by_epsg:
                bounds_by_epsg[epsg] = transform_bounds(region.bounds, region.crs, f'EPSG:{epsg}')
            minx, miny, maxx, maxy = bounds_by_epsg[epsg]
            if not (easting < maxx and easting + TILE_GRID > minx and northing < maxy and northing + TILE_GRID > miny):
                continue

            original = os.path.basename(urlparse(url).path)
            filename = self._add_suffix(original, self.filename_suffix)
            request = DownloadRequest(
                region_id=region_id,
                asset_id=os.path.splitext(original)[0],
                url=url,
                filename=filename,
                kind=AssetKind.RASTER,
                product=ProductType.DEM,
                metadata={'tile_key': parsed},
            )
            kept.append((parsed, request))
        return kept

    @staticmethod
    def _add_suffix(filename: str, suffix: str | None) -> str:
        if not suffix:
            return filename
        name, extension = os.path.splitext(filename)
        return f'{name}_{suffix}{extension}'
