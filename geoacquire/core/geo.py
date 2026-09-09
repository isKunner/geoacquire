#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: geo.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Small CRS helpers shared by independent geospatial Sources.

import math
from functools import lru_cache

from pyproj import CRS, Transformer


@lru_cache(maxsize=128)
def horizontal_crs(crs: str | int | CRS) -> CRS:
    """Return the horizontal component used for tile selection and XY warping.

    Accept an authority code, WKT/PROJ string, or pyproj CRS. Compound CRSs
    bundle horizontal and vertical definitions; selecting the horizontal part
    does not transform elevation values. A purely vertical CRS raises ValueError.

    Example:
        >>> horizontal_crs('EPSG:9518').to_epsg()  # WGS 84 + EGM2008 height
        4326
    """
    parsed = CRS.from_user_input(crs)
    if parsed.is_compound:
        horizontal = next(
            (
                sub_crs
                for sub_crs in parsed.sub_crs_list
                if sub_crs.is_geographic or sub_crs.is_projected
            ),
            None,
        )
        if horizontal is None:
            raise ValueError(f'No horizontal CRS component found in: {parsed.name}')
        return horizontal
    if parsed.is_geographic or parsed.is_projected:
        return parsed
    raise ValueError(f'CRS is not horizontal and has no horizontal component: {parsed.name}')


@lru_cache(maxsize=128)
def _bounds_transformer(source: CRS, target: CRS) -> Transformer:
    """Reuse the few CRS pairs shared by thousands of target/tile comparisons."""
    return Transformer.from_crs(source, target, always_xy=True)


def transform_bounds(
    bounds: tuple[float, float, float, float],
    source_crs: str | CRS,
    target_crs: str | CRS,
) -> tuple[float, float, float, float]:
    """Map (minx, miny, maxx, maxy) from source to target horizontal CRS.

    Edges are densified for non-linear transforms. Axis order is always XY,
    including longitude/latitude for geographic CRSs; non-finite output fails
    here before it reaches a spatial query. No vertical conversion is performed.
    """
    source = horizontal_crs(source_crs)
    target = horizontal_crs(target_crs)
    transformed = bounds
    if source != target:
        transformer = _bounds_transformer(source, target)
        transformed = transformer.transform_bounds(*bounds, densify_pts=21)
    if not all(math.isfinite(value) for value in transformed):
        raise ValueError(
            f'CRS transform produced non-finite bounds from {source.name} to {target.name}: '
            f'{transformed}. Check the local PROJ data/grid files.'
        )
    return transformed
