#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: vector.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: Polygon vector inputs converted to geometry-aware Regions.

import os

import geopandas as gpd

from geoacquire.core.geo import horizontal_crs
from geoacquire.core.models import Region, RegionMetadataKey

from .base import BaseRegionProvider


class VectorRegionProvider(BaseRegionProvider):
    """Read every Polygon/MultiPolygon feature as one independent Region."""

    def __init__(
        self,
        source: str,
        id_field: str | None = None,
        crs_override: str | None = None,
        layer: str | None = None,
    ):
        if not source:
            raise ValueError('Vector source must not be empty')
        if id_field is not None and not id_field.strip():
            raise ValueError('id_field must be null or a non-empty field name')
        if crs_override is not None and not crs_override.strip():
            raise ValueError('crs_override must be null or a non-empty CRS')
        self.source = source
        self.id_field = id_field
        self.crs_override = crs_override
        self.layer = layer

    def get_regions(self) -> dict[str, Region]:
        if not os.path.isfile(self.source):
            raise ValueError(f'Vector Region source does not exist: {self.source}')
        frame = gpd.read_file(self.source, layer=self.layer)
        if frame.empty:
            raise ValueError(f'Vector Region source contains no features: {self.source}')
        if self.crs_override is not None:
            frame = frame.set_crs(self.crs_override, allow_override=True)
        if frame.crs is None:
            raise ValueError(
                f'Vector Region source has no CRS: {self.source}; set crs_override explicitly'
            )
        crs = horizontal_crs(frame.crs)
        bounds = tuple(float(value) for value in frame.total_bounds)
        if crs.is_geographic and (
            bounds[0] < -180.0 or bounds[2] > 180.0
            or bounds[1] < -90.0 or bounds[3] > 90.0
        ):
            raise ValueError(
                f'Vector coordinates {bounds} are impossible for declared geographic CRS '
                f'{crs.to_string()}; the .prj is likely incorrect. Set crs_override to the '
                'actual projected CRS.'
            )
        if self.id_field is not None and self.id_field not in frame.columns:
            raise ValueError(
                f'Vector id_field {self.id_field!r} is missing; available fields: '
                f'{sorted(column for column in frame.columns if column != frame.geometry.name)}'
            )

        regions: dict[str, Region] = {}
        for ordinal, (feature_index, row) in enumerate(frame.iterrows()):
            geometry = row.geometry
            if geometry is None or geometry.is_empty:
                raise ValueError(f'Vector feature {feature_index!r} has empty geometry')
            if geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
                raise ValueError(
                    f'Vector feature {feature_index!r} is {geometry.geom_type}; '
                    'only Polygon and MultiPolygon are supported'
                )
            if not geometry.is_valid:
                raise ValueError(f'Vector feature {feature_index!r} has invalid geometry')
            region_id = self._region_id(row, ordinal)
            if region_id in regions:
                raise ValueError(f'Vector Region IDs are not unique: {region_id!r}')
            regions[region_id] = Region(
                region_id=region_id,
                bounds=tuple(float(value) for value in geometry.bounds),
                crs=crs.to_string(),
                metadata={
                    RegionMetadataKey.GEOMETRY_WKB_HEX: geometry.wkb_hex,
                    'vector_source': os.path.abspath(self.source),
                    'vector_feature_index': str(feature_index),
                    'vector_geometry_type': geometry.geom_type,
                },
            )
        return regions

    def _region_id(self, row, ordinal: int) -> str:
        if self.id_field is None:
            return f'feature_{ordinal}'
        value = row[self.id_field]
        if value is None or str(value).strip() == '':
            raise ValueError(f'Vector feature {ordinal} has an empty {self.id_field!r} value')
        return str(value).strip()
