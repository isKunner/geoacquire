#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: bounds.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Region provider for coordinate bounds, a target raster, or a target raster directory.

import os
from typing import Any

from geoacquire.core.models import Region

from .base import BaseRegionProvider
from .reader import _RasterDirectorySnapshot, _scan_raster_directory, read_raster_region


class BoundsRegionProvider(BaseRegionProvider):
    """
    Auto-detect three kinds of input and convert each into one or more Region objects.

    Supported input types for ``source``:
      1. Coordinate bounds  -- a list/tuple of four floats ``[minx, miny, maxx, maxy]``
         or a dict of named bounds. Used when no local raster is available.
      2. Single raster file -- absolute or relative path to one ``.tif/.tiff``.
         The file's own CRS, transform, and shape become the Region metadata.
      3. Raster directory   -- path to a folder containing one or more target rasters.
         Each raster becomes an independent Region. Useful for batch-processing an
         entire state or dataset.

    The provider never downloads data; it only inspects local files or wraps raw
    coordinates into the canonical ``Region`` contract expected by Sources and
    Postprocessors.
    """

    def __init__(
        self,
        source: Any,
        region: str | list[str] | None = None,
        crs: str = 'EPSG:4326',
        exclude_suffixes: list[str] | None = None,
        skip_existing_suffixes: list[str] | None = None,
    ):
        """
        Initialize the provider with input specification and naming rules.

        Args:
            source:
                The primary geographic input. Three shapes are accepted:

                - **Coordinate bounds** (``list`` / ``tuple`` / ``dict``):
                  A 4-element sequence ``[minx, miny, maxx, maxy]`` in the CRS
                  given by ``crs``; or a dict ``{'name': {'bounds': [...], 'crs': '...'}}``
                  for multiple named regions.
                - **File path** (``str``): Path to a single ``.tif`` or ``.tiff``.
                  The file must exist and carry a valid CRS.
                - **Directory path** (``str``): Path to a folder. Every
                  ``.tif/.tiff`` directly inside becomes a Region. Subdirectories
                  are not scanned; callers that manage directory trees should run
                  one pipeline for each input/output directory pair.

                The constructor does not open rasters immediately; resolution is
                deferred to ``get_regions()``.

            region:
                Explicit name(s) for the output Region ID(s).

                - When ``source`` is a **single file** or **coordinate bounds**:
                  provide a ``str`` (e.g., ``'AL_0'``). If ``None``, the stem of
                  the filename is used for files, or ``'region_0'`` for anonymous
                  bounds.
                - When ``source`` is a **directory**: provide a ``list[str]`` with
                  one name per raster found, in the same sorted order the scanner
                  produces. If ``None``, names are derived automatically from
                  filename stems.
                - A single ``str`` is **not** allowed for directory inputs; the
                  constructor raises ``ValueError`` in that case.

            crs:
                Coordinate reference system used when ``source`` is raw coordinate
                bounds. Ignored when ``source`` is a raster file or directory,
                because the CRS is read from the raster itself. Defaults to
                ``'EPSG:4326'`` (WGS 84).

            exclude_suffixes:
                Only relevant when ``source`` is a directory. A list of filename
                suffixes (with or without leading underscore) that cause matching
                rasters to be excluded from the directory snapshot's target list.
                Example: ``['lidar']`` drops ``0_lidar.tif``. This is
                typically used to filter out result files from a previous pipeline
                run so they are not mistaken for new targets.

            skip_existing_suffixes:
                Only relevant when ``source`` is a directory. After the directory
                scan produces a candidate list, each raster is checked for sibling
                files whose stems end with these suffixes. If **all** suffixes are
                found, the raster is skipped entirely and never becomes a Region.
                Example: ``['_lidar']`` means: if ``0_lidar.tif`` already exists
                beside ``0.tif``, do not process ``0.tif`` again. When every
                candidate is skipped, ``get_regions()`` prints a message and
                returns an empty dict.

        Raises:
            ValueError: If a directory input receives a single ``str`` for
                ``region``.
        """
        if isinstance(region, str) and not region.strip():
            raise ValueError('region must be null or a non-empty string/list')
        self.source = source
        self.region = region
        self.crs = crs
        self.exclude_suffixes = exclude_suffixes
        self.skip_existing_suffixes = skip_existing_suffixes

    def get_regions(self) -> dict[str, Region]:
        if isinstance(self.source, str):
            if os.path.isdir(self.source):
                return self._from_dir(self.source)
            if os.path.isfile(self.source):
                return self._from_file(self.source)
            raise ValueError(f'Region source path does not exist: {self.source}')
        return self._from_bounds(self.source)

    def _from_file(self, path: str) -> dict[str, Region]:
        if isinstance(self.region, list):
            if len(self.region) != 1:
                raise ValueError('A single target raster requires exactly one region name')
            name = self.region[0]
        else:
            name = (
                self.region
                if self.region is not None
                else os.path.splitext(os.path.basename(path))[0]
            )
        return {str(name): read_raster_region(path, str(name))}

    def _from_dir(self, directory: str) -> dict[str, Region]:
        snapshot = _scan_raster_directory(directory)
        paths = snapshot.raster_paths(
            exclude_suffixes=[
                *(self.exclude_suffixes or []),
                *(self.skip_existing_suffixes or []),
            ],
        )
        if not paths:
            raise ValueError(f'No target rasters found in: {directory}')
        if isinstance(self.region, str):
            raise ValueError('A target raster directory requires region to be a list or null')
        if isinstance(self.region, list) and not self.region:
            raise ValueError('region must be null or a non-empty list for a target raster directory')
        names = (
            self.region
            if self.region is not None
            else [os.path.splitext(os.path.basename(path))[0] for path in paths]
        )
        if len(names) != len(paths):
            raise ValueError(f'region contains {len(names)} names, but {len(paths)} rasters were found')
        if len(set(str(name) for name in names)) != len(names):
            raise ValueError(
                'Target raster names are not unique; provide an explicit region list'
            )

        pending = [
            (str(name), path)
            for name, path in zip(names, paths)
            if not self._has_completed_siblings(path, snapshot)
        ]
        if not pending:
            print(f'[regions] all {len(paths)} target rasters already have requested sibling outputs')
        return {name: read_raster_region(path, name) for name, path in pending}

    def _has_completed_siblings(self, path: str, snapshot: _RasterDirectorySnapshot) -> bool:
        stem, _ = os.path.splitext(path)
        suffixes = self.skip_existing_suffixes or []
        if not suffixes:
            return False
        for suffix in suffixes:
            normalized = suffix if suffix.startswith('_') else f'_{suffix}'
            if not snapshot.contains_filename(os.path.basename(f'{stem}{normalized}.tif')):
                return False
        return True

    def _from_bounds(self, source: Any) -> dict[str, Region]:
        if isinstance(self.region, list):
            raise ValueError('Coordinate input accepts one region name, not a list')

        if isinstance(source, dict) and 'bounds' not in source:
            regions = {}
            for name, value in source.items():
                item = value if isinstance(value, dict) else {'bounds': value}
                regions[str(name)] = self._pack(str(name), item['bounds'], item.get('crs', self.crs))
            return regions

        item = source if isinstance(source, dict) else {'bounds': source}
        name = str(
            self.region
            if self.region is not None
            else item.get('name', 'region_0')
        )
        return {name: self._pack(name, item['bounds'], item.get('crs', self.crs))}

    @staticmethod
    def _pack(region_id: str, bounds: Any, crs: str) -> Region:
        # Region is the single validation boundary for length, ordering, and
        # finite coordinates. The provider only normalizes external values.
        return Region(region_id=region_id, bounds=tuple(float(value) for value in bounds), crs=crs)
