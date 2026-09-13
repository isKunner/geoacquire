#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Point-vector input expanded to temporary metre-based acquisition Regions."""

import math
import os
from typing import Any

import geopandas as gpd
from pyproj import CRS, Transformer

from geoacquire.core.geo import horizontal_crs, transform_bounds
from geoacquire.core.models import Region, RegionMetadataKey

from .base import BaseRegionProvider


class PointRegionProvider(BaseRegionProvider):
    """Create one query Region per Point without modifying the source vector.

    ``query_size_m`` controls only source discovery. A later point-window
    postprocessor may use a smaller final size, for example a 500 m query around
    a point followed by a native-grid 448 m crop.
    """

    def __init__(
        self,
        source: str,
        id_field: str | None = None,
        query_size_m: float = 500.0,
        include_values: dict[str, list[str]] | None = None,
        crs_override: str | None = None,
        layer: str | None = None,
    ):
        if not source:
            raise ValueError('Point vector source must not be empty')
        if id_field is not None and not id_field.strip():
            raise ValueError('id_field must be null or a non-empty field name')
        if not math.isfinite(query_size_m) or query_size_m <= 0:
            raise ValueError('query_size_m must be a finite value greater than zero')
        if crs_override is not None and not crs_override.strip():
            raise ValueError('crs_override must be null or a non-empty CRS')
        filters = include_values or {}
        for field, values in filters.items():
            if not field or not values:
                raise ValueError('include_values requires non-empty fields and value lists')
        self.source = source
        self.id_field = id_field
        self.query_size_m = float(query_size_m)
        self.include_values = {
            str(field): frozenset(str(value).strip() for value in values)
            for field, values in filters.items()
        }
        self.crs_override = crs_override
        self.layer = layer

    def get_regions(self) -> dict[str, Region]:
        if not os.path.isfile(self.source):
            raise ValueError(f'Point vector source does not exist: {self.source}')
        frame = gpd.read_file(self.source, layer=self.layer)
        if frame.empty:
            raise ValueError(f'Point vector source contains no features: {self.source}')
        if self.crs_override is not None:
            frame = frame.set_crs(self.crs_override, allow_override=True)
        if frame.crs is None:
            raise ValueError(
                f'Point vector source has no CRS: {self.source}; set crs_override explicitly'
            )
        crs = horizontal_crs(frame.crs)
        if self.id_field is not None and self.id_field not in frame.columns:
            raise ValueError(
                f'Point id_field {self.id_field!r} is missing; available fields: '
                f'{self._attribute_fields(frame)}'
            )
        for field in self.include_values:
            if field not in frame.columns:
                raise ValueError(
                    f'Point filter field {field!r} is missing; available fields: '
                    f'{self._attribute_fields(frame)}'
                )

        regions: dict[str, Region] = {}
        selected = 0
        for ordinal, (feature_index, row) in enumerate(frame.iterrows()):
            geometry = row.geometry
            if geometry is None or geometry.is_empty:
                raise ValueError(f'Point feature {feature_index!r} has empty geometry')
            if geometry.geom_type != 'Point':
                raise ValueError(
                    f'Point feature {feature_index!r} is {geometry.geom_type}; only Point is supported'
                )
            if not geometry.is_valid:
                raise ValueError(f'Point feature {feature_index!r} has invalid geometry')
            if not self._included(row):
                continue
            selected += 1
            region_id = self._region_id(row, ordinal)
            if region_id in regions:
                raise ValueError(f'Point Region IDs are not unique: {region_id!r}')
            point = (float(geometry.x), float(geometry.y))
            properties = {
                str(column): self._plain_value(row[column])
                for column in frame.columns
                if column != frame.geometry.name
            }
            regions[region_id] = Region(
                region_id=region_id,
                bounds=self._query_bounds(point, crs),
                crs=crs.to_string(),
                metadata={
                    RegionMetadataKey.POINT_X: point[0],
                    RegionMetadataKey.POINT_Y: point[1],
                    RegionMetadataKey.POINT_CRS: crs.to_string(),
                    RegionMetadataKey.QUERY_SIZE_M: self.query_size_m,
                    'vector_source': os.path.abspath(self.source),
                    'vector_feature_index': str(feature_index),
                    'vector_geometry_type': 'Point',
                    'vector_properties': properties,
                },
            )
        if selected == 0:
            raise ValueError('Point vector filters selected no features')
        return regions

    def _query_bounds(self, point: tuple[float, float], crs: CRS) -> tuple[float, float, float, float]:
        half = self.query_size_m / 2.0
        if crs.is_projected:
            axes = crs.axis_info
            if len(axes) < 2:
                raise ValueError(f'Projected CRS has no two horizontal axes: {crs.name}')
            factors = [float(axis.unit_conversion_factor or 0.0) for axis in axes[:2]]
            if any(not math.isfinite(value) or value <= 0 for value in factors):
                raise ValueError(f'Projected CRS has invalid linear units: {crs.name}')
            return (
                point[0] - half / factors[0],
                point[1] - half / factors[1],
                point[0] + half / factors[0],
                point[1] + half / factors[1],
            )

        # Geographic inputs remain in their declared CRS. Build a local metric
        # square around the point and convert only its envelope back to that CRS.
        to_wgs84 = Transformer.from_crs(crs, 'EPSG:4326', always_xy=True)
        longitude, latitude = to_wgs84.transform(*point)
        geod = CRS.from_epsg(4326).get_geod()
        west = geod.fwd(longitude, latitude, 270.0, half)
        east = geod.fwd(longitude, latitude, 90.0, half)
        south = geod.fwd(longitude, latitude, 180.0, half)
        north = geod.fwd(longitude, latitude, 0.0, half)
        if west[0] > east[0]:
            raise ValueError('Point query window crosses the antimeridian; split it explicitly')
        wgs84_bounds = (west[0], south[1], east[0], north[1])
        return transform_bounds(wgs84_bounds, 'EPSG:4326', crs)

    def _included(self, row) -> bool:
        return all(
            str(row[field]).strip() in accepted
            for field, accepted in self.include_values.items()
        )

    def _region_id(self, row, ordinal: int) -> str:
        if self.id_field is None:
            return f'feature_{ordinal}'
        value = row[self.id_field]
        if value is None or str(value).strip() == '':
            raise ValueError(f'Point feature {ordinal} has an empty {self.id_field!r} value')
        return str(value).strip()

    @staticmethod
    def _plain_value(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, 'item'):
            value = value.item()
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    @staticmethod
    def _attribute_fields(frame) -> list[str]:
        return sorted(column for column in frame.columns if column != frame.geometry.name)
