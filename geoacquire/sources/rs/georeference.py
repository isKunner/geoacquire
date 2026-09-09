#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: georeference.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Materialize XYZ image tiles as correctly georeferenced Web-Mercator GeoTIFFs.

import os

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_bounds

from .tile_math import parse_tile_name, tile_bounds_web_mercator


class XYZGeoReferencer:
    """Write the native XYZ pixel grid in EPSG:3857 instead of approximating it in EPSG:4326."""

    @staticmethod
    def convert(source_path: str, destination_path: str) -> None:
        zoom, x, y = parse_tile_name(os.path.basename(source_path))
        bounds = tile_bounds_web_mercator(zoom, x, y)
        with Image.open(source_path) as image:
            array = np.asarray(image.convert('RGB'))
        height, width = array.shape[:2]
        temporary = destination_path + '.part'
        try:
            with rasterio.open(
                temporary,
                'w',
                driver='GTiff',
                height=height,
                width=width,
                count=3,
                dtype='uint8',
                crs='EPSG:3857',
                transform=from_bounds(*bounds, width, height),
                compress='lzw',
            ) as dataset:
                for band in range(3):
                    dataset.write(array[:, :, band], band + 1)
            os.replace(temporary, destination_path)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)
