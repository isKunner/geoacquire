#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: wayback.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Esri World Imagery Wayback Source with lazy release-date resolution.

import re
from datetime import date as date_type

import requests

from .xyz import XYZSource


class WaybackSource(XYZSource):
    """Select one imagery release lazily, then use the ordinary XYZ file pipeline.

    date=None selects the newest release; a requested date selects the nearest
    release (earlier on ties). This is a release date, not each tile's capture date.
    """

    DEFAULT_SERVER = (
        'https://wayback.maptiles.arcgis.com/arcgis/rest/services/'
        'World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/{version}/{z}/{y}/{x}'
    )
    CONFIG_URL = 'https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json'
    TITLE_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')

    def __init__(
        self,
        date: str | None = None,
        version: int | None = None,
        release_date: str | None = None,
        server: str = DEFAULT_SERVER,
        zoom: int = 18,
        tile_ext: str = 'jpg',
        output_format: str = 'geotiff',
        keep_download: bool = True,
    ):
        super().__init__(zoom, tile_ext, output_format, keep_download)
        if version is not None and release_date is None:
            raise ValueError('release_date is required when version is set explicitly')
        self.requested_date = date
        self.version = version
        self.release_date = release_date
        self.server = server

    def _ensure_release(self) -> None:
        if self.version is not None:
            return
        response = requests.get(self.CONFIG_URL, timeout=(30, 60))
        response.raise_for_status()
        releases: list[tuple[date_type, int]] = []
        for version, info in response.json().items():
            match = self.TITLE_DATE_RE.search(info.get('itemTitle', ''))
            if match:
                releases.append((date_type(*map(int, match.groups())), int(version)))
        if not releases:
            raise RuntimeError('No dated Wayback releases were found in the official configuration')

        if self.requested_date is None:
            release, version = max(releases)
        else:
            target = date_type.fromisoformat(self.requested_date)
            release, version = min(releases, key=lambda item: (abs((item[0] - target).days), item[0]))
        self.version = version
        self.release_date = release.isoformat()
        print(f'[wayback] date={self.requested_date or "latest"} release={self.release_date} version={self.version}')

    def build_url(self, zoom: int, x: int, y: int) -> str:
        self._ensure_release()
        return self.server.format(version=self.version, z=zoom, x=x, y=y)

    def build_filename(self, zoom: int, x: int, y: int) -> str:
        self._ensure_release()
        tag = self.release_date.replace('-', '')[2:]
        return f'z{zoom:02d}_x{x:06d}_y{y:06d}_{tag}_wayback.{self.tile_ext}'
