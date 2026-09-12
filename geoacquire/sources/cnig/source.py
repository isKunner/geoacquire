#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Spain PNOA third-coverage native DTM COG downloader."""

import math
import re
from collections.abc import Iterable

from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.models import AssetSpec, DownloadRequest, Region
from geoacquire.core.region_geometry import positive_area_intersection, region_polygon
from geoacquire.core.source import HTTPSource

from .client import CNIGClient, CNIGTile
from .products import get_product_spec


class CNIGMDTSource(HTTPSource):
    """Download native PNOA MDT COGs selected by true target polygons."""

    ANONYMOUS_DOWNLOAD_LIMIT = 20
    _ZONE = re.compile(r'-(ETRS89|REGCAN95)-H(\d{2})-', re.IGNORECASE)

    def __init__(
        self,
        product: str = 'mdt50cm',
        footprint_cache_path: str = './cache/cnig/mdt50cm_footprints.json',
        query_batch_size: int = 100,
        footprint_workers: int = 4,
        request_timeout: float = 60.0,
    ):
        if query_batch_size < 1:
            raise ValueError('query_batch_size must be >= 1')
        self.product_spec = get_product_spec(product)
        self.query_batch_size = query_batch_size
        self.client = CNIGClient(
            self.product_spec,
            footprint_cache_path=footprint_cache_path,
            footprint_workers=footprint_workers,
            request_timeout=request_timeout,
        )

    @property
    def output_specs(self) -> frozenset[AssetSpec]:
        return frozenset({AssetSpec(self.product_spec.kind, self.product_spec.product)})

    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        geometries = {
            region_id: region_polygon(region, 'EPSG:4326')
            for region_id, region in regions.items()
        }
        candidates: dict[str, CNIGTile] = {}
        region_geometries = list(geometries.values())
        for start in range(0, len(region_geometries), self.query_batch_size):
            combined = unary_union(region_geometries[start:start + self.query_batch_size])
            for tile in self.client.query_tiles([combined]):
                candidates[tile.sequential] = tile

        selected = self._select_tiles(geometries, tuple(candidates.values()))
        if len(selected) > self.ANONYMOUS_DOWNLOAD_LIMIT:
            raise RuntimeError(
                f'CNIG selected {len(selected)} unique files, but its official anonymous '
                f'limit is {self.ANONYMOUS_DOWNLOAD_LIMIT}. Account login is not implemented '
                'yet; reduce the test area instead of bypassing the portal limit.'
            )

        product = self.product_spec
        for tile, targets in selected:
            source_crs = self._source_crs(tile.filename)
            yield DownloadRequest(
                region_id=targets[0],
                asset_id=f'cnig:{product.key}:{tile.sequential}',
                url=self.client.DOWNLOAD_URL,
                filename=tile.filename,
                kind=product.kind,
                product=product.product,
                headers={
                    'User-Agent': self.client.USER_AGENT,
                    'Referer': self.client.product_url,
                },
                method='POST',
                data={'secDescDirLA': tile.sequential},
                metadata={
                    'sequential': tile.sequential,
                    'bounds_wgs84': tile.footprint.bounds,
                    'portal_product': product.key,
                    'resolution': product.resolution,
                    'source_crs': source_crs,
                    'vertical_crs': None,
                    'vertical_datum': 'orthometric',
                    'license': 'CC BY 4.0',
                    'native_cog': True,
                },
                target_region_ids=targets,
            )

    def plan_requests(
        self,
        regions: dict[str, Region],
        progress: AcquisitionProgress,
    ) -> Iterable[DownloadRequest]:
        yield from self.build_requests(regions)
        for region_id in regions:
            progress.close_region(region_id)

    def _select_tiles(
        self,
        regions: dict[str, BaseGeometry],
        tiles: tuple[CNIGTile, ...],
    ) -> list[tuple[CNIGTile, tuple[str, ...]]]:
        if not tiles:
            for region_id in regions:
                print(f'[cnig/mdt50cm] no current files intersect region={region_id}')
            return []

        footprints = tuple(tile.footprint for tile in tiles)
        index = STRtree(footprints)
        targets_by_tile: dict[int, list[str]] = {}
        for region_id, target in regions.items():
            matches = [
                int(value)
                for value in index.query(target, predicate='intersects')
                if positive_area_intersection(target, footprints[int(value)])
            ]
            grouped: dict[str, list[int]] = {}
            for value in matches:
                grouped.setdefault(self._logical_tile_key(tiles[value].filename), []).append(value)
            for aliases in grouped.values():
                chosen = self._choose_zone_alias(target, tiles, aliases)
                targets_by_tile.setdefault(chosen, []).append(region_id)
            if not grouped:
                print(f'[cnig/mdt50cm] no current files intersect region={region_id}')

        return [
            (tiles[index_value], tuple(dict.fromkeys(targets)))
            for index_value, targets in sorted(
                targets_by_tile.items(),
                key=lambda item: tiles[item[0]].filename,
            )
        ]

    def _choose_zone_alias(
        self,
        target: BaseGeometry,
        tiles: tuple[CNIGTile, ...],
        aliases: list[int],
    ) -> int:
        preferred_zone = min(60, max(1, math.floor((target.centroid.x + 180.0) / 6.0) + 1))
        for index_value in sorted(aliases, key=lambda value: tiles[value].filename):
            match = self._ZONE.search(tiles[index_value].filename)
            if match and int(match.group(2)) == preferred_zone:
                return index_value
        return min(aliases, key=lambda value: tiles[value].filename)

    @classmethod
    def _logical_tile_key(cls, filename: str) -> str:
        return cls._ZONE.sub('-ZONE-', filename, count=1).upper()

    @classmethod
    def _source_crs(cls, filename: str) -> str | None:
        match = cls._ZONE.search(filename)
        if match is None:
            return None
        datum, raw_zone = match.groups()
        zone = int(raw_zone)
        if datum.upper() == 'ETRS89':
            return f'EPSG:{25800 + zone}'
        if datum.upper() == 'REGCAN95' and zone == 28:
            return 'EPSG:4083'
        return None
