#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Geometry helpers shared by geometry-aware Source planners."""

from functools import lru_cache

from pyproj import CRS, Transformer
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.wkb import loads as load_wkb

from .geo import transform_bounds
from .models import Region


def region_polygon(region: Region, target_crs: str) -> BaseGeometry:
    """Return a Region as a valid Polygon/MultiPolygon in ``target_crs``.

    Vector-backed Regions retain their true geometry as WKB. Bounds-only and
    raster-backed Regions intentionally fall back to their transformed extent,
    preserving the historical planning contract for those input modes.
    """
    if region.geometry_wkb_hex is None:
        return box(*transform_bounds(region.bounds, region.crs, target_crs))

    try:
        geometry = load_wkb(bytes.fromhex(region.geometry_wkb_hex))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f'Region {region.region_id!r} has invalid geometry_wkb_hex'
        ) from exc
    _validate_polygon(region, geometry, 'input')

    source = CRS.from_user_input(region.crs)
    target = CRS.from_user_input(target_crs)
    if source != target:
        geometry = transform(_transformer(source.srs, target.srs).transform, geometry)
    _validate_polygon(region, geometry, target.to_string())
    return geometry


def positive_area_intersection(left: BaseGeometry, right: BaseGeometry) -> bool:
    """Return True only when polygon interiors overlap, not just their edges."""
    return left.intersection(right).area > 0.0


def _validate_polygon(region: Region, geometry: BaseGeometry, stage: str) -> None:
    if geometry.is_empty or geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
        raise ValueError(
            f'Region {region.region_id!r} {stage} geometry must be a non-empty '
            'Polygon/MultiPolygon'
        )
    if not geometry.is_valid:
        raise ValueError(f'Region {region.region_id!r} {stage} geometry is invalid')


@lru_cache(maxsize=64)
def _transformer(source_crs: str, target_crs: str) -> Transformer:
    return Transformer.from_crs(source_crs, target_crs, always_xy=True)
