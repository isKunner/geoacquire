#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""HTTP client for CNIG's polygon-aware download portal endpoints."""

import html
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import requests
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

from .products import CNIGProductSpec


@dataclass(frozen=True)
class CNIGTile:
    sequential: str
    filename: str
    geometry: dict[str, Any]

    @property
    def footprint(self) -> BaseGeometry:
        return shape(self.geometry)


class CNIGClient:
    """Query file references and footprints without browser automation."""

    BASE_URL = 'https://centrodedescargas.cnig.es/CentroDescargas/'
    QUERY_URL = BASE_URL + 'archivosSerie'
    FOOTPRINT_URL = BASE_URL + 'localizarCoordsSec'
    DOWNLOAD_URL = BASE_URL + 'descargaDir'
    USER_AGENT = 'GeoAcquire/0.1 (+https://centrodedescargas.cnig.es/)'

    def __init__(
        self,
        product: CNIGProductSpec,
        footprint_cache_path: str,
        footprint_workers: int = 4,
        request_timeout: float = 60.0,
    ):
        if footprint_workers < 1:
            raise ValueError('footprint_workers must be >= 1')
        if request_timeout <= 0:
            raise ValueError('request_timeout must be > 0')
        self.product = product
        self.footprint_cache_path = footprint_cache_path
        self.footprint_workers = footprint_workers
        self.request_timeout = request_timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': self.USER_AGENT,
            'X-Requested-With': 'XMLHttpRequest',
            'Accept-Language': 'es-ES,es;q=0.9',
        })

    @property
    def product_url(self) -> str:
        return self.BASE_URL + self.product.page_slug

    def query_tiles(self, geometries: list[BaseGeometry]) -> tuple[CNIGTile, ...]:
        """Return current portal files intersecting any supplied WGS84 geometry."""
        references: dict[str, str] = {}
        for geometry in geometries:
            for sequential, filename in self._query_geometry(geometry):
                existing = references.get(sequential)
                if existing is not None and existing != filename:
                    raise RuntimeError(
                        f'CNIG sequential {sequential} maps to conflicting filenames: '
                        f'{existing!r} and {filename!r}'
                    )
                references[sequential] = filename

        cache = self._read_footprint_cache()
        missing = [sequential for sequential in references if sequential not in cache]
        if missing:
            with ThreadPoolExecutor(
                max_workers=self.footprint_workers,
                thread_name_prefix='cnig-footprint',
            ) as executor:
                footprints = executor.map(self._read_footprint, missing)
                for sequential, geometry in zip(missing, footprints):
                    cache[sequential] = geometry
            self._write_footprint_cache(cache)

        tiles = [
            CNIGTile(sequential, filename, cache[sequential])
            for sequential, filename in references.items()
        ]
        return tuple(sorted(tiles, key=lambda tile: (tile.filename, tile.sequential)))

    def _query_geometry(self, geometry: BaseGeometry) -> list[tuple[str, str]]:
        feature_collection = {
            'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'properties': {}, 'geometry': mapping(geometry)}],
        }
        coordinates = json.dumps(feature_collection, separators=(',', ':'))
        results: dict[str, str] = {}
        page = 1
        while True:
            response = self.session.post(
                self.QUERY_URL,
                data={
                    'numPagina': str(page),
                    'codAgr': self.product.group_code,
                    'codSerie': self.product.series_code,
                    'coordenadas': coordinates,
                },
                headers={'Referer': self.product_url},
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            parser = _CNIGFileTableParser(self.product.filename_prefix)
            parser.feed(response.text)
            page_results = parser.results
            for sequential, filename in page_results:
                results[sequential] = filename
            total = _result_total(response.text)
            if not page_results or len(results) >= total:
                break
            page += 1
            if page > 2000:
                raise RuntimeError('CNIG catalogue pagination exceeded 2000 pages')
        return list(results.items())

    def _read_footprint(self, sequential: str) -> dict[str, Any]:
        response = requests.post(
            self.FOOTPRINT_URL,
            data={'secuencial': sequential},
            headers={
                'User-Agent': self.USER_AGENT,
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': self.product_url,
            },
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        value = response.json()
        collection = json.loads(value['coordsJson'])
        features = collection.get('features') or []
        if len(features) != 1 or not isinstance(features[0].get('geometry'), dict):
            raise ValueError(f'CNIG file {sequential} returned no unique footprint')
        geometry = features[0]['geometry']
        footprint = shape(geometry)
        if footprint.is_empty or footprint.geom_type not in {'Polygon', 'MultiPolygon'}:
            raise ValueError(f'CNIG file {sequential} returned an invalid footprint')
        return geometry

    def _read_footprint_cache(self) -> dict[str, dict[str, Any]]:
        if not os.path.isfile(self.footprint_cache_path):
            return {}
        try:
            with open(self.footprint_cache_path, 'r', encoding='utf-8') as file:
                value = json.load(file)
            if value.get('schema_version') != 1:
                return {}
            footprints = value.get('footprints')
            return footprints if isinstance(footprints, dict) else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_footprint_cache(self, footprints: dict[str, dict[str, Any]]) -> None:
        destination = os.path.abspath(self.footprint_cache_path)
        directory = os.path.dirname(destination)
        os.makedirs(directory, exist_ok=True)
        payload = {'schema_version': 1, 'footprints': footprints}
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=directory,
            prefix=os.path.basename(destination) + '.',
            suffix='.tmp',
            delete=False,
        ) as file:
            temporary = file.name
            json.dump(payload, file, ensure_ascii=False, separators=(',', ':'))
        try:
            os.replace(temporary, destination)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)


class _CNIGFileTableParser(HTMLParser):
    """Extract desktop table filename/sequential pairs from one HTML fragment."""

    _SEQUENTIAL = re.compile(r'(?:target|linkDescDir(?:S3)?)_(\d+)$', re.IGNORECASE)

    def __init__(self, filename_prefix: str):
        super().__init__(convert_charrefs=True)
        self.filename_prefix = filename_prefix.upper()
        self.results: list[tuple[str, str]] = []
        self._in_row = False
        self._row_depth = 0
        self._text: list[str] = []
        self._sequential: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == 'tr':
            self._in_row = True
            self._row_depth = 1
            self._text = []
            self._sequential = None
            return
        if not self._in_row:
            return
        if tag.lower() == 'tr':
            self._row_depth += 1
        for name, value in attrs:
            if name.lower() != 'id' or not value:
                continue
            match = self._SEQUENTIAL.match(value)
            if match:
                self._sequential = match.group(1)

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_row or tag.lower() != 'tr':
            return
        self._row_depth -= 1
        if self._row_depth > 0:
            return
        text = html.unescape(' '.join(self._text))
        filename = next(
            (
                token.strip()
                for token in re.findall(r'[^\s<>"\']+\.tif(?:f)?', text, re.IGNORECASE)
                if token.upper().startswith(self.filename_prefix)
            ),
            None,
        )
        if filename and self._sequential:
            pair = (self._sequential, filename)
            if pair not in self.results:
                self.results.append(pair)
        self._in_row = False


def _result_total(value: str) -> int:
    match = re.search(
        r'id=["\']totalArchivos["\'][^>]*value=["\'](\d+)["\']',
        value,
        re.IGNORECASE,
    )
    if match is None:
        raise ValueError('CNIG catalogue response has no totalArchivos value')
    return int(match.group(1))
