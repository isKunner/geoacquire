#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: reader.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Raster metadata readers used by target-based Region providers.

import os
from dataclasses import dataclass

import rasterio

from geoacquire.core.models import Region, RegionMetadataKey

RASTER_EXTENSIONS = ('.tif', '.tiff')


@dataclass(frozen=True)
class _RasterDirectorySnapshot:
    """All regular files observed during one directory enumeration.

    ``regular_files`` is the unfiltered source of truth: generated outputs stay
    in it even when suffix rules remove them from the target list.
    ``filenames`` contains normalized basenames for completed-sibling checks.
    Subdirectories are not scanned, so a parent-directory index is unnecessary.
    """

    regular_files: tuple[str, ...]
    filenames: frozenset[str]

    def raster_paths(
        self,
        extensions: tuple[str, ...] = RASTER_EXTENSIONS,
        exclude_suffixes: list[str] | None = None,
    ) -> list[str]:
        """Return sorted target rasters after extension and suffix filtering."""
        normalized_extensions = frozenset(extension.lower() for extension in extensions)
        normalized_suffixes = tuple(
            suffix.lower() if suffix.startswith('_') else f'_{suffix.lower()}'
            for suffix in (exclude_suffixes or [])
        )

        paths = []
        for path in self.regular_files:
            stem, extension = os.path.splitext(os.path.basename(path))
            if extension.lower() not in normalized_extensions:
                continue
            if normalized_suffixes and stem.lower().endswith(normalized_suffixes):
                continue
            paths.append(path)
        return sorted(paths)

    def contains_filename(self, filename: str) -> bool:
        """Check one exact basename without filesystem I/O."""
        return os.path.normcase(filename) in self.filenames


def _scan_raster_directory(directory: str) -> _RasterDirectorySnapshot:
    """Enumerate one directory once and build an in-memory reusable snapshot.

    A batch interface can invoke one pipeline per input/output directory pair.
    ``DirEntry.is_file`` identifies
    regular files during this single scan; all later extension, suffix, and
    completed-sibling checks operate on the snapshot without more filesystem
    existence calls.
    """
    regular_files: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if entry.is_file():
                    regular_files.append(entry.path)
            except OSError:
                # Broken or inaccessible entries are not usable rasters.
                continue

    return _RasterDirectorySnapshot(
        regular_files=tuple(regular_files),
        filenames=frozenset(os.path.normcase(os.path.basename(path)) for path in regular_files),
    )


def read_raster_region(path: str, region_id: str) -> Region:
    """Read the target grid without loading raster pixels.

    Output metadata uses shape=(rows, columns) and transform=(a,b,c,d,e,f,0,0,1):
    x=a*column+b*row+c, y=d*column+e*row+f. For 0.tif, target_path is absolute;
    postprocessors use this saved grid rather than reopening the target pixels.
    """
    absolute_path = os.path.abspath(path)
    with rasterio.open(absolute_path) as dataset:
        if dataset.crs is None:
            raise ValueError(f'No CRS found in target raster: {absolute_path}')
        bounds = dataset.bounds
        metadata = {
            RegionMetadataKey.TARGET_PATH: absolute_path,
            RegionMetadataKey.SHAPE: (dataset.height, dataset.width),
            RegionMetadataKey.TRANSFORM: tuple(dataset.transform),
        }
        return Region(
            region_id=region_id,
            bounds=(bounds.left, bounds.bottom, bounds.right, bounds.top),
            crs=dataset.crs.to_string(),
            metadata=metadata,
        )
