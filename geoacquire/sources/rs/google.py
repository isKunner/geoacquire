#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: google.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Google satellite imagery XYZ Source.

from .xyz import XYZSource


class GoogleSource(XYZSource):
    """Supply Google-style URL/filename templates; XYZSource owns tile conversion."""

    DEFAULT_SERVER = 'http://arcmap.googlecnapps.club/maps/vt?lyrs=s&x={x}&y={y}&z={z}&s=Ga'

    def __init__(
        self,
        server: str = DEFAULT_SERVER,
        zoom: int = 18,
        tile_ext: str = 'jpg',
        output_format: str = 'geotiff',
        keep_download: bool = True,
    ):
        super().__init__(zoom, tile_ext, output_format, keep_download)
        self.server = server

    def build_url(self, zoom: int, x: int, y: int) -> str:
        return self.server.format(z=zoom, x=x, y=y)

    def build_filename(self, zoom: int, x: int, y: int) -> str:
        return f'z{zoom:02d}_x{x:06d}_y{y:06d}_google.{self.tile_ext}'
