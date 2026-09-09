#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: planner.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Plan Copernicus DEM one-degree geocells from a Region.

import math

from geoacquire.core.geo import transform_bounds
from geoacquire.core.models import Region


def plan_region_tiles(region: Region, resolution: str = '30') -> list[str]:
    """Return half-open one-degree tiles; exact maximum edges do not add touching neighbors."""
    if resolution not in ('30', '90'):
        raise ValueError("resolution must be '30' or '90'")
    left, bottom, right, top = transform_bounds(region.bounds, region.crs, 'EPSG:4326')
    if left < -180 or right > 180 or bottom < -90 or top > 90:
        raise ValueError(f'Region exceeds valid longitude/latitude bounds: {(left, bottom, right, top)}')

    lon_start, lon_stop = math.floor(left), math.ceil(right)
    lat_start, lat_stop = math.floor(bottom), math.ceil(top)
    arc = '10' if resolution == '30' else '30'
    tiles: list[str] = []
    for latitude in range(lat_start, lat_stop):
        north_south = 'N' if latitude >= 0 else 'S'
        for longitude in range(lon_start, lon_stop):
            east_west = 'E' if longitude >= 0 else 'W'
            tiles.append(
                f'Copernicus_DSM_{arc}_{north_south}{abs(latitude):02d}_00_'
                f'{east_west}{abs(longitude):03d}_00_DEM'
            )
    return tiles
