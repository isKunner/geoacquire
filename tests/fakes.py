#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: fakes.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Test-only class_path components for local lifecycle smoke tests.

from collections.abc import Iterable

from geoacquire.core.models import DownloadRequest, Region
from geoacquire.core.source import HTTPSource


class StaticHTTPSource(HTTPSource):
    def __init__(self, url: str, filename: str = 'asset.bin'):
        self.url = url
        self.filename = filename

    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        for region_id in regions:
            yield DownloadRequest(
                region_id=region_id,
                asset_id='static_asset',
                url=self.url,
                filename=self.filename,
            )
