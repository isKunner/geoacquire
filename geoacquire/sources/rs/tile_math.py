#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: tile_math.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Spherical-Mercator XYZ tile math and filename parsing.

import math
import re

from geoacquire.core.geo import transform_bounds

MAX_MERCATOR_LAT = 85.05112878
WEB_MERCATOR_HALF_WORLD = 20037508.342789244
TILE_NAME_RE = re.compile(r'z(\d+)_x(\d+)_y(\d+)')


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Convert longitude/latitude degrees to bounded XYZ (x,y) indexes at zoom."""
    lat = min(max(lat, -MAX_MERCATOR_LAT), MAX_MERCATOR_LAT)
    count = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * count)
    y = int(
        (1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi)
        / 2.0
        * count
    )
    return max(0, min(count - 1, x)), max(0, min(count - 1, y))


def tile_bounds_wgs84(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) in EPSG:4326 degrees for an XYZ tile."""
    count = 2 ** zoom
    minx = x / count * 360.0 - 180.0
    maxx = (x + 1) / count * 360.0 - 180.0
    maxy = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / count))))
    miny = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / count))))
    return minx, miny, maxx, maxy


def tile_bounds_web_mercator(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (minx, miny, maxx, maxy) in EPSG:3857 metres for georeferencing."""
    count = 2 ** zoom
    tile_size = 2.0 * WEB_MERCATOR_HALF_WORLD / count
    minx = -WEB_MERCATOR_HALF_WORLD + x * tile_size
    maxx = minx + tile_size
    maxy = WEB_MERCATOR_HALF_WORLD - y * tile_size
    miny = maxy - tile_size
    return minx, miny, maxx, maxy


def parse_tile_name(filename: str) -> tuple[int, int, int]:
    """Read (zoom,x,y): z02_x000002_y000001_google.jpg -> (2,2,1)."""
    match = TILE_NAME_RE.search(filename)
    if not match:
        raise ValueError(f'Cannot parse z/x/y from filename: {filename}')
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def bounds_to_wgs84(
    bounds: tuple[float, float, float, float],
    crs: str,
) -> tuple[float, float, float, float]:
    """Convert a source-CRS bbox to (west, south, east, north) in degrees."""
    return transform_bounds(bounds, crs, 'EPSG:4326')
