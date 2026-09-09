#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: base.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Shared WESM query, link-list cache, and cross-workunit deduplication for USGS Sources.

import hashlib
import os
import re
import time
import tempfile
from abc import abstractmethod
from collections import OrderedDict
from collections.abc import Iterable

import requests

from geoacquire.core.models import DownloadRequest, Region
from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.source import HTTPSource

from .wesm import WESMClient


class USGSHTTPSource(HTTPSource):
    """Shared WESM, link-cache, and target-directory batching capabilities."""

    def __init__(
        self,
        wesm_cache_dir: str = './cache/wesm',
        link_cache_dir: str = './cache/link_lists',
        link_cache_days: float = 10.0,
    ):
        self.wesm = WESMClient(cache_dir=wesm_cache_dir)
        self.link_cache_dir = link_cache_dir
        self.link_cache_days = link_cache_days
        self._text_cache: dict[str, str] = {}

    def _fetch_text(self, url: str) -> str:
        if url in self._text_cache:
            return self._text_cache[url]

        path = self.link_cache_path(url)
        fresh = os.path.isfile(path) and time.time() - os.path.getmtime(path) < self.link_cache_days * 86400
        if fresh:
            with open(path, 'r', encoding='utf-8') as file:
                text = file.read()
        else:
            response = requests.get(url, timeout=(30, 120))
            response.raise_for_status()
            text = response.text
            os.makedirs(self.link_cache_dir, exist_ok=True)
            # Separate state processes share this cache. Give each writer its
            # own temporary file; replace only after closing the complete text.
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=self.link_cache_dir,
                prefix=os.path.basename(path) + '.', suffix='.tmp', delete=False,
            ) as file:
                temporary = file.name
                file.write(text)
            try:
                os.replace(temporary, path)
            except PermissionError:
                # Windows can deny a simultaneous replace even with unique
                # temporaries. Another writer's complete cache is sufficient;
                # do not let cache contention discard freshly fetched links.
                if not os.path.isfile(path):
                    raise
            finally:
                if os.path.isfile(temporary):
                    os.remove(temporary)

        self._text_cache[url] = text
        return text

    def link_cache_path(self, url: str) -> str:
        """Return the shared disk-cache key, also used by offline audit replay."""
        parent = re.sub(r'[^\w.-]', '_', url.rstrip('/').split('/')[-2])
        digest = hashlib.md5(url.encode('utf-8')).hexdigest()[:8]
        return os.path.join(self.link_cache_dir, f'{parent}_{digest}.txt')

    @staticmethod
    def normalize_link(url: str) -> str:
        """Repair the single-slash scheme found in official El Paso link lists."""
        return url.strip().replace('https:/rockyweb.usgs.gov/', 'https://rockyweb.usgs.gov/', 1)

    @staticmethod
    def _region_batches(regions: dict[str, Region]) -> list[tuple[str, dict[str, Region]]]:
        """Group target rasters by parent directory; keep free bboxes independent."""
        grouped: OrderedDict[str, dict[str, Region]] = OrderedDict()
        labels: dict[str, str] = {}
        for region_id, region in regions.items():
            target_path = region.target_path
            if target_path:
                parent = os.path.abspath(os.path.dirname(target_path))
                key = os.path.normcase(parent)
                label = parent
            else:
                key = f'region:{region_id}'
                label = region_id
            grouped.setdefault(key, {})[region_id] = region
            labels[key] = label
        return [(labels[key], batch) for key, batch in grouped.items()]


class USGSWorkunitHTTPSource(USGSHTTPSource):
    """Template for products planned independently per Region and WESM workunit.

    USGSDEM1mSource uses this template. LiDAR deliberately stays on the thinner
    USGSHTTPSource because it plans one shared state pool across many Regions.
    """

    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        return self._plan_workunits(regions)

    def plan_requests(self, regions: dict[str, Region], progress: AcquisitionProgress) -> Iterable[DownloadRequest]:
        return self._plan_workunits(regions, progress)

    def _plan_workunits(
        self,
        regions: dict[str, Region],
        progress: AcquisitionProgress | None = None,
    ) -> Iterable[DownloadRequest]:
        """Query adjacent target rasters together, then lazily yield requests."""
        for label, batch in self._region_batches(regions):
            print(f'[usgs/plan] querying batch={label} regions={len(batch)}')
            workunits_by_region = self.wesm.query_many(batch)
            for region_id, region in batch.items():
                yield from self._build_region_requests(
                    region_id,
                    region,
                    workunits_by_region[region_id],
                )
                if progress is not None:
                    progress.close_region(region_id)

    def _build_region_requests(
        self,
        region_id: str,
        region: Region,
        workunits: list[dict],
    ) -> list[DownloadRequest]:
        tiles: dict[tuple, tuple[str, str, DownloadRequest]] = {}
        plain: list[DownloadRequest] = []
        for workunit in workunits:
            for key, request in self._build_workunit_requests(region_id, region, workunit):
                if key is None:
                    plain.append(request)
                    continue
                date = str(workunit.get('collect_end') or '')
                workunit_name = str(workunit.get('workunit') or 'unknown')
                previous = tiles.get(key)
                if previous is None or date > previous[0]:
                    if previous is not None:
                        print(
                            f'[usgs] duplicate tile {key}: kept {workunit_name} ({date}), '
                            f'dropped {previous[1]} ({previous[0]})'
                        )
                    tiles[key] = (date, workunit_name, request)
                else:
                    print(
                        f'[usgs] duplicate tile {key}: kept {previous[1]} ({previous[0]}), '
                        f'dropped {workunit_name} ({date})'
                    )
        planned = plain + [item[2] for item in tiles.values()]
        if workunits and not planned:
            print(f'[usgs] no link-list tiles matched region={region_id}; naming may differ')
        return planned

    @abstractmethod
    def _build_workunit_requests(
        self,
        region_id: str,
        region: Region,
        workunit: dict,
    ) -> list[tuple[tuple | None, DownloadRequest]]:
        pass
