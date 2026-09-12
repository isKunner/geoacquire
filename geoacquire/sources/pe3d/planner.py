#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: planner.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: Plan PE3D 1:5,000 quadrangles from arbitrary Region bounds.

import math
from dataclasses import dataclass

from shapely.geometry import box

from geoacquire.core.geo import transform_bounds
from geoacquire.core.models import Region
from geoacquire.core.region_geometry import region_polygon


_LON_STEP = 1.0 / 32.0  # 1 minute 52.5 seconds of longitude
_LAT_STEP = 1.0 / 48.0  # 1 minute 15 seconds of latitude
_HALF_MILLION = (('V', 'X'), ('Y', 'Z'))
_QUARTER_MILLION = (('A', 'B'), ('C', 'D'))
_HUNDRED_THOUSAND = (('I', 'II', 'III'), ('IV', 'V', 'VI'))
_FIFTY_THOUSAND = (('1', '2'), ('3', '4'))
_TWENTY_FIVE_THOUSAND = (('NO', 'NE'), ('SO', 'SE'))
_TEN_THOUSAND = (('A', 'B'), ('C', 'D'), ('E', 'F'))
_FIVE_THOUSAND = (('I', 'II'), ('III', 'IV'))


@dataclass(frozen=True)
class PE3DSheet:
    """One nominal 1:5,000 map sheet before the raster's edge overlap."""

    code: str
    bounds_wgs84: tuple[float, float, float, float]


def sheet_for_point(longitude: float, latitude: float) -> PE3DSheet:
    """Return the Brazilian systematic 1:5,000 sheet containing one point.

    PE3D lies south of the equator. Its raster files extend slightly beyond the
    nominal sheet boundary, but their names follow this geographic hierarchy.
    """
    if not -180.0 <= longitude < 180.0:
        raise ValueError(f'longitude must be in [-180, 180), got {longitude}')
    if not -88.0 < latitude < 0.0:
        raise ValueError(f'PE3D sheet planning requires a southern latitude, got {latitude}')

    lon_column = math.floor((longitude + 180.0) / _LON_STEP)
    lat_row = math.floor((-latitude) / _LAT_STEP)
    zone = lon_column // 192 + 1
    band_index = lat_row // 192
    if band_index >= 22:
        raise ValueError(f'latitude is outside supported CIM bands: {latitude}')

    x = lon_column % 192
    y = lat_row % 192
    tokens = [f'S{chr(ord("A") + band_index)}', f'{zone:02d}']

    column, row = x // 96, y // 96
    tokens.append(_HALF_MILLION[row][column])
    x, y = x % 96, y % 96

    column, row = x // 48, y // 48
    tokens.append(_QUARTER_MILLION[row][column])
    x, y = x % 48, y % 48

    column, row = x // 16, y // 24
    tokens.append(_HUNDRED_THOUSAND[row][column])
    x, y = x % 16, y % 24

    column, row = x // 8, y // 12
    tokens.append(_FIFTY_THOUSAND[row][column])
    x, y = x % 8, y % 12

    column, row = x // 4, y // 6
    tokens.append(_TWENTY_FIVE_THOUSAND[row][column])
    x, y = x % 4, y % 6

    column, row = x // 2, y // 2
    tokens.append(_TEN_THOUSAND[row][column])
    x, y = x % 2, y % 2
    tokens.append(_FIVE_THOUSAND[y][x])

    west = -180.0 + lon_column * _LON_STEP
    east = west + _LON_STEP
    north = -lat_row * _LAT_STEP
    south = north - _LAT_STEP
    return PE3DSheet('-'.join(tokens), (west, south, east, north))


def plan_region_sheets(
    region: Region,
    min_intersection_fraction: float = 0.0,
) -> list[PE3DSheet]:
    """Return 1:5,000 sheets intersecting a Region's bounds or true geometry."""
    if not 0.0 <= min_intersection_fraction <= 1.0:
        raise ValueError('min_intersection_fraction must be between 0 and 1')
    left, bottom, right, top = transform_bounds(region.bounds, region.crs, 'EPSG:4326')
    if left < -180.0 or right > 180.0 or bottom <= -88.0 or top >= 0.0:
        raise ValueError(
            'PE3D Region must be south of the equator and inside valid longitude bounds: '
            f'{(left, bottom, right, top)}'
        )

    lon_start = math.floor((left + 180.0) / _LON_STEP)
    lon_stop = math.ceil((right + 180.0) / _LON_STEP)
    row_start = math.floor((-top) / _LAT_STEP)
    row_stop = math.ceil((-bottom) / _LAT_STEP)

    geometry = region_polygon(region, 'EPSG:4326') if region.geometry_wkb_hex else None
    sheets: list[PE3DSheet] = []
    for row in range(row_start, row_stop):
        latitude = -(row + 0.5) * _LAT_STEP
        for column in range(lon_start, lon_stop):
            longitude = -180.0 + (column + 0.5) * _LON_STEP
            sheet = sheet_for_point(longitude, latitude)
            if geometry is None or _meaningfully_intersects(
                geometry,
                sheet,
                min_intersection_fraction,
            ):
                sheets.append(sheet)
    return sheets


def _meaningfully_intersects(geometry, sheet: PE3DSheet, minimum: float) -> bool:
    tile = box(*sheet.bounds_wgs84)
    intersection_area = geometry.intersection(tile).area
    if intersection_area <= 0.0:
        return False
    if minimum <= 0.0:
        return True
    reference_area = min(geometry.area, tile.area)
    return reference_area > 0.0 and intersection_area / reference_area >= minimum
