#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: dem_1m.py
# @Time    : 2026/9/1
# @Author  : Kevin
# @Describe: New Zealand national LiDAR 1 m DEM from public LINZ COGs.

import os
from collections.abc import Iterable
from urllib.parse import urlparse

from shapely.geometry import box, shape
from shapely.strtree import STRtree

from geoacquire.core.geo import transform_bounds
from geoacquire.core.models import AssetKind, AssetSpec, DownloadRequest, ProductType, Region
from geoacquire.core.source import HTTPSource

from .catalog import LINZSTACTile, LINZStaticSTACCatalog


NZ_DEM_1M_COLLECTION = (
    'https://nz-elevation.s3-ap-southeast-2.amazonaws.com/'
    'new-zealand/new-zealand/dem_1m/2193/collection.json'
)


class LINZDEM1mSource(HTTPSource):
    """Select and download LINZ's public New Zealand LiDAR 1 m DEM COGs.

    Item footprints and COG links come from the official static STAC collection.
    Selection happens in WGS84; downloaded rasters retain native NZTM2000
    (EPSG:2193) coordinates and NZVD2016 orthometric elevations.
    """

    OUTPUT_SPECS = frozenset({AssetSpec(AssetKind.RASTER, ProductType.DEM)})

    def __init__(
        self,
        collection_url: str = NZ_DEM_1M_COLLECTION,
        catalog_cache_path: str = './cache/linz/nz_dem_1m_stac.json',
        catalog_cache_days: float = 10.0,
        catalog_workers: int = 16,
        filename_suffix: str | None = 'nz_dem_1m',
    ):
        self.filename_suffix = filename_suffix
        self.catalog = LINZStaticSTACCatalog(
            collection_url=collection_url,
            cache_path=catalog_cache_path,
            cache_days=catalog_cache_days,
            max_workers=catalog_workers,
        )
        self._tiles: tuple[LINZSTACTile, ...] | None = None
        self._tile_index: STRtree | None = None

    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        tiles, tile_index = self._spatial_index()
        for region_id, region in regions.items():
            bounds_wgs84 = transform_bounds(region.bounds, region.crs, 'EPSG:4326')
            target = box(*bounds_wgs84)
            matched = 0
            for tile_index_value in sorted(int(value) for value in tile_index.query(target, predicate='intersects')):
                tile = tiles[tile_index_value]
                matched += 1
                original = os.path.basename(urlparse(tile.url).path) or f'{tile.tile_id}.tiff'
                filename = self._add_suffix(original, self.filename_suffix)
                yield DownloadRequest(
                    region_id=region_id,
                    asset_id=f'linz:nz_dem_1m:{tile.tile_id}',
                    url=tile.url,
                    filename=filename,
                    kind=AssetKind.RASTER,
                    product=ProductType.DEM,
                    metadata={
                        'tile_id': tile.tile_id,
                        'bounds_wgs84': tile.bounds_wgs84,
                        'item_url': tile.item_url,
                        'capture_start': tile.start_datetime,
                        'capture_end': tile.end_datetime,
                        'checksum': tile.checksum,
                        'resolution': 1.0,
                        'source_crs': 'EPSG:2193',
                        'vertical_crs': 'EPSG:7839',
                        'vertical_datum': 'NZVD2016',
                        'license': 'CC BY 4.0',
                    },
                )
            if matched == 0:
                print(f'[linz/dem_1m] no STAC tiles intersect region={region_id}')

    def _spatial_index(self) -> tuple[tuple[LINZSTACTile, ...], STRtree]:
        """Build one real R-tree for repeated Region-to-LINZ-tile searches."""
        if self._tiles is None or self._tile_index is None:
            self._tiles = tuple(self.catalog.list_tiles())
            self._tile_index = STRtree([shape(tile.geometry) for tile in self._tiles])
        return self._tiles, self._tile_index

    @staticmethod
    def _add_suffix(filename: str, suffix: str | None) -> str:
        if not suffix:
            return filename
        name, extension = os.path.splitext(filename)
        return f'{name}_{suffix}{extension}'
