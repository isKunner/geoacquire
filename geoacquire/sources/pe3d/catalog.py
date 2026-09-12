#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: catalog.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: Cached official PE3D quadrangle IDs, footprints, and portal selections.

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

from .planner import sheet_for_point


class PE3DCatalogError(RuntimeError):
    """The official quadrangle catalogue could not be loaded or validated."""


@dataclass(frozen=True)
class PE3DQuadrangle:
    sheet_code: str
    portal_id: str
    municipality: str
    color: str
    bounds_wgs84: tuple[float, float, float, float]

    @property
    def selection_value(self) -> str:
        """Exact string assembled by mapa.php for its id[] form values."""
        return f'{self.portal_id} - {self.municipality}'


class PE3DQuadrangleCatalog:
    """Lazy public GeoJSON catalogue with an atomic, expiring local cache."""

    URL = 'https://pe3d.pe.gov.br/quadriculas_pe.json'
    AVAILABLE_COLORS = frozenset({'green', '#9acd32'})

    def __init__(
        self,
        session: requests.Session,
        cache_path: str = './cache/pe3d/quadriculas_pe.json',
        cache_days: float = 10.0,
        verify_tls: bool | str = True,
    ):
        if not cache_path:
            raise ValueError('PE3D catalog cache_path must not be empty')
        if cache_days < 0:
            raise ValueError('PE3D catalog cache_days must be >= 0')
        self.session = session
        self.cache_path = cache_path
        self.cache_days = cache_days
        self.verify_tls = verify_tls
        self._by_sheet: dict[str, PE3DQuadrangle] | None = None

    def by_sheet(self) -> dict[str, PE3DQuadrangle]:
        if self._by_sheet is None:
            self._by_sheet = self.parse(self._load_data())
        return self._by_sheet

    def _load_data(self) -> dict[str, Any]:
        cached = self._read_cache()
        if cached is not None and self._cache_is_fresh():
            return cached
        try:
            response = self.session.get(
                self.URL,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; GeoAcquire/0.1)'},
                verify=self.verify_tls,
                timeout=(30, 120),
            )
            response.raise_for_status()
            data = response.json()
            # Validate the full response before replacing a usable old cache.
            self.parse(data)
            self._write_cache(data)
            return data
        except (requests.RequestException, ValueError, TypeError, PE3DCatalogError) as exc:
            if cached is not None:
                return cached
            raise PE3DCatalogError(f'PE3D quadrangle catalogue unavailable: {exc}') from exc

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            with open(self.cache_path, 'r', encoding='utf-8') as stream:
                data = json.load(stream)
            self.parse(data)
            return data
        except (FileNotFoundError, OSError, ValueError, TypeError, PE3DCatalogError):
            return None

    def _cache_is_fresh(self) -> bool:
        try:
            age_seconds = max(0.0, time.time() - os.path.getmtime(self.cache_path))
        except OSError:
            return False
        return age_seconds <= self.cache_days * 86400.0

    def _write_cache(self, data: dict[str, Any]) -> None:
        absolute = os.path.abspath(self.cache_path)
        os.makedirs(os.path.dirname(absolute), exist_ok=True)
        temporary = absolute + '.part'
        try:
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(',', ':'))
            os.replace(temporary, absolute)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)

    @classmethod
    def parse(cls, data: Any) -> dict[str, PE3DQuadrangle]:
        if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
            raise PE3DCatalogError('PE3D quadrangle catalogue is not a FeatureCollection')
        features = data.get('features')
        if not isinstance(features, list):
            raise PE3DCatalogError('PE3D quadrangle catalogue has no feature list')

        by_sheet: dict[str, PE3DQuadrangle] = {}
        for feature in features:
            try:
                properties = feature['properties']
                color = str(properties['color']).lower()
                if color not in cls.AVAILABLE_COLORS:
                    continue
                portal_id = str(properties['id_quad']).strip()
                municipality = str(properties['name']).strip()
                ring = feature['geometry']['coordinates'][0]
                coordinates = [(float(point[0]), float(point[1])) for point in ring]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise PE3DCatalogError('Invalid PE3D quadrangle feature') from exc
            if not portal_id or not municipality or len(coordinates) < 4:
                raise PE3DCatalogError('PE3D quadrangle feature has empty identity or geometry')
            xs = [point[0] for point in coordinates]
            ys = [point[1] for point in coordinates]
            bounds = (min(xs), min(ys), max(xs), max(ys))
            longitude = (bounds[0] + bounds[2]) / 2.0
            latitude = (bounds[1] + bounds[3]) / 2.0
            sheet_code = sheet_for_point(longitude, latitude).code
            item = PE3DQuadrangle(sheet_code, portal_id, municipality, color, bounds)
            previous = by_sheet.get(sheet_code)
            if previous is None:
                by_sheet[sheet_code] = item
            elif previous.portal_id != item.portal_id:
                raise PE3DCatalogError(
                    f'PE3D sheet {sheet_code} has conflicting portal IDs '
                    f'{previous.portal_id!r} and {item.portal_id!r}'
                )
            # Duplicate polygons represent one sheet intersecting two municipalities.
            # The portal ID is stable, so retain the first exact UI selection string.
        if not by_sheet:
            raise PE3DCatalogError('PE3D quadrangle catalogue contains no available sheets')
        return by_sheet
