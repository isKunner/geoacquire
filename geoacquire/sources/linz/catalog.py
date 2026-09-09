#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: catalog.py
# @Time    : 2026/9/1
# @Author  : Kevin
# @Describe: Cached reader for LINZ's public static STAC elevation catalogue.

import json
import math
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


@dataclass(frozen=True)
class LINZSTACTile:
    """One downloadable COG and the WGS84 footprint from its STAC Item."""

    tile_id: str
    bounds_wgs84: tuple[float, float, float, float]
    geometry: dict[str, Any]
    url: str
    item_url: str
    start_datetime: str | None = None
    end_datetime: str | None = None
    checksum: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'LINZSTACTile':
        bounds = tuple(float(number) for number in value['bounds_wgs84'])
        if len(bounds) != 4 or not all(math.isfinite(number) for number in bounds):
            raise ValueError(f'Invalid LINZ tile bounds: {bounds}')
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ValueError(f'Invalid LINZ tile bounds order: {bounds}')
        geometry = value.get('geometry')
        if not isinstance(geometry, dict) or not geometry.get('type'):
            raise ValueError(f'LINZ tile {value.get("tile_id")} has no valid geometry')
        return cls(
            tile_id=str(value['tile_id']),
            bounds_wgs84=bounds,
            geometry=geometry,
            url=str(value['url']),
            item_url=str(value['item_url']),
            start_datetime=value.get('start_datetime'),
            end_datetime=value.get('end_datetime'),
            checksum=value.get('checksum'),
        )


class LINZStaticSTACCatalog:
    """Create and reuse a compact spatial index for a static STAC collection.

    The LINZ collection document links to one small JSON Item per 1:50,000 map
    sheet. The first run reads those Items concurrently and stores their COG URLs
    and footprints locally. Normal runs only read this single cache file.
    """

    CACHE_SCHEMA_VERSION = 1
    REQUEST_ATTEMPTS = 3
    REQUEST_TIMEOUT = (30, 120)
    USER_AGENT = 'GeoAcquire/0.1 (+https://data.linz.govt.nz/)'

    def __init__(
        self,
        collection_url: str,
        cache_path: str = './cache/linz/nz_dem_1m_stac.json',
        cache_days: float = 10.0,
        max_workers: int = 16,
    ):
        if not collection_url:
            raise ValueError('collection_url must not be empty')
        if not cache_path:
            raise ValueError('cache_path must not be empty')
        if cache_days < 0:
            raise ValueError('cache_days must be >= 0')
        if max_workers < 1:
            raise ValueError('max_workers must be >= 1')
        self.collection_url = collection_url
        self.cache_path = cache_path
        self.cache_days = cache_days
        self.max_workers = max_workers
        self._tiles: tuple[LINZSTACTile, ...] | None = None

    def list_tiles(self) -> tuple[LINZSTACTile, ...]:
        """Return a deterministic tile index, refreshing expired disk state."""
        if self._tiles is not None:
            return self._tiles

        fresh = (
            os.path.isfile(self.cache_path)
            and time.time() - os.path.getmtime(self.cache_path) < self.cache_days * 86400
        )
        if fresh:
            try:
                self._tiles = self._read_cache()
                return self._tiles
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                print(f'[linz/stac] invalid cache={self.cache_path}; refreshing: {exc}')

        try:
            self._tiles = self._refresh()
        except Exception as exc:
            if not os.path.isfile(self.cache_path):
                raise
            print(f'[linz/stac] refresh failed; using stale cache={self.cache_path}: {exc}')
            self._tiles = self._read_cache()
        return self._tiles

    def _refresh(self) -> tuple[LINZSTACTile, ...]:
        collection = self._request_json(self.collection_url)
        raw_links = collection.get('links')
        if not isinstance(raw_links, list):
            raise ValueError('LINZ STAC Collection has no links list')

        item_urls: list[str] = []
        seen: set[str] = set()
        for link in raw_links:
            if not isinstance(link, dict) or link.get('rel') != 'item' or not link.get('href'):
                continue
            item_url = urljoin(self.collection_url, str(link['href']))
            if item_url not in seen:
                seen.add(item_url)
                item_urls.append(item_url)
        if not item_urls:
            raise ValueError('LINZ STAC Collection contains no Item links')

        print(f'[linz/stac] indexing items={len(item_urls)} workers={self.max_workers}')
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix='linz-stac') as executor:
            tiles = list(executor.map(self._read_item, item_urls))
        tiles.sort(key=lambda tile: tile.tile_id)
        result = tuple(tiles)
        self._write_cache(result)
        print(f'[linz/stac] index ready tiles={len(result)} cache={self.cache_path}')
        return result

    def _read_item(self, item_url: str) -> LINZSTACTile:
        item = self._request_json(item_url)
        tile_id = str(item.get('id') or '').strip()
        if not tile_id:
            raise ValueError(f'STAC Item has no id: {item_url}')

        raw_bounds = item.get('bbox')
        if not isinstance(raw_bounds, list) or len(raw_bounds) < 4:
            raise ValueError(f'STAC Item {tile_id} has no 2D bbox')
        bounds = tuple(float(number) for number in raw_bounds[:4])
        geometry = item.get('geometry')
        if not isinstance(geometry, dict):
            raise ValueError(f'STAC Item {tile_id} has no geometry')

        assets = item.get('assets')
        if not isinstance(assets, dict):
            raise ValueError(f'STAC Item {tile_id} has no assets')
        asset = assets.get('visual')
        if not self._is_tiff_asset(asset):
            asset = next((candidate for candidate in assets.values() if self._is_tiff_asset(candidate)), None)
        if not isinstance(asset, dict):
            raise ValueError(f'STAC Item {tile_id} has no GeoTIFF asset')

        properties = item.get('properties') or {}
        capture = properties.get('datetime')
        return LINZSTACTile.from_dict({
            'tile_id': tile_id,
            'bounds_wgs84': bounds,
            'geometry': geometry,
            'url': urljoin(item_url, str(asset['href'])),
            'item_url': item_url,
            'start_datetime': properties.get('start_datetime') or capture,
            'end_datetime': properties.get('end_datetime') or capture,
            'checksum': asset.get('file:checksum'),
        })

    @staticmethod
    def _is_tiff_asset(asset: object) -> bool:
        if not isinstance(asset, dict) or not asset.get('href'):
            return False
        media_type = str(asset.get('type') or '').lower()
        extension = os.path.splitext(urlparse(str(asset['href'])).path)[1].lower()
        return 'tiff' in media_type or extension in ('.tif', '.tiff')

    def _request_json(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.REQUEST_ATTEMPTS):
            try:
                response = requests.get(
                    url,
                    headers={'User-Agent': self.USER_AGENT, 'Accept': 'application/json'},
                    timeout=self.REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict):
                    raise ValueError(f'Expected a JSON object from {url}')
                return value
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.REQUEST_ATTEMPTS:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f'Failed to read LINZ STAC JSON after {self.REQUEST_ATTEMPTS} attempts: {url}') from last_error

    def _read_cache(self) -> tuple[LINZSTACTile, ...]:
        with open(self.cache_path, 'r', encoding='utf-8') as file:
            value = json.load(file)
        if value.get('schema_version') != self.CACHE_SCHEMA_VERSION:
            raise ValueError('unsupported cache schema')
        if value.get('collection_url') != self.collection_url:
            raise ValueError('cache belongs to a different STAC Collection')
        raw_tiles = value.get('tiles')
        if not isinstance(raw_tiles, list) or not raw_tiles:
            raise ValueError('cache contains no tiles')
        tiles = tuple(LINZSTACTile.from_dict(tile) for tile in raw_tiles)
        return tuple(sorted(tiles, key=lambda tile: tile.tile_id))

    def _write_cache(self, tiles: tuple[LINZSTACTile, ...]) -> None:
        destination = os.path.abspath(self.cache_path)
        directory = os.path.dirname(destination)
        os.makedirs(directory, exist_ok=True)
        payload = {
            'schema_version': self.CACHE_SCHEMA_VERSION,
            'collection_url': self.collection_url,
            'tiles': [asdict(tile) for tile in tiles],
        }
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=directory,
            prefix=os.path.basename(destination) + '.', suffix='.tmp', delete=False,
        ) as file:
            temporary = file.name
            json.dump(payload, file, ensure_ascii=False, separators=(',', ':'))
        try:
            os.replace(temporary, destination)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)
