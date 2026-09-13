#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Public Copernicus DEM COG acquisition from the AWS Open Data registry."""

import os
from collections.abc import Iterable

from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.models import AssetKind, AssetSpec, DownloadRequest, ProductType, Region
from geoacquire.core.source import HTTPSource

from .planner import plan_region_tiles


class CopDEMPublicCOGSource(HTTPSource):
    """Download anonymous GLO-30/GLO-90 2021 COG tiles from public AWS S3."""

    OUTPUT_SPECS = frozenset({AssetSpec(AssetKind.RASTER, ProductType.DEM)})
    DEFAULT_BASE_URLS = {
        '30': 'https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com',
        '90': 'https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com',
    }

    def __init__(
        self,
        resolution: str = '30',
        base_url: str | None = None,
        filename_suffix: str | None = None,
    ):
        if resolution not in ('30', '90'):
            raise ValueError("resolution must be '30' or '90'")
        if base_url is not None and not base_url.strip():
            raise ValueError('base_url must be null or a non-empty URL')
        if filename_suffix is not None and (
            not filename_suffix.strip() or os.path.basename(filename_suffix) != filename_suffix
        ):
            raise ValueError('filename_suffix must be null or a path component')
        self.resolution = resolution
        self.base_url = (base_url or self.DEFAULT_BASE_URLS[resolution]).rstrip('/')
        self.filename_suffix = filename_suffix

    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        targets_by_tile: dict[str, list[str]] = {}
        for region_id, region in regions.items():
            for tile in plan_region_tiles(region, self.resolution):
                targets_by_tile.setdefault(tile, []).append(region_id)

        for tile in sorted(targets_by_tile):
            targets = tuple(dict.fromkeys(targets_by_tile[tile]))
            cog_tile = tile.replace('Copernicus_DSM_', 'Copernicus_DSM_COG_', 1)
            filename = f'{cog_tile}.tif'
            if self.filename_suffix is not None:
                filename = f'{cog_tile}_{self.filename_suffix}.tif'
            yield DownloadRequest(
                region_id=targets[0],
                asset_id=f'copdem:public_cog:{self.resolution}:{cog_tile}',
                url=f'{self.base_url}/{cog_tile}/{cog_tile}.tif',
                filename=filename,
                kind=AssetKind.RASTER,
                product=ProductType.DEM,
                metadata={
                    'tile_id': cog_tile,
                    'resolution': self.resolution,
                    'release': '2021',
                    'provider': 'AWS Open Data',
                    'format': 'COG',
                },
                target_region_ids=targets,
            )

    def plan_requests(
        self,
        regions: dict[str, Region],
        progress: AcquisitionProgress,
    ) -> Iterable[DownloadRequest]:
        """Plan all Regions together so every one-degree COG is transferred once."""
        yield from self.build_requests(regions)
        for region_id in regions:
            progress.close_region(region_id)
