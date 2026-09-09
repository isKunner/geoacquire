#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: lidar_rasterizer.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Materialize USGS LAZ point clouds as DSM and DTM GeoTIFF products.

import os
from dataclasses import dataclass

import laspy
import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.transform import from_origin

GROUND_CLASS = 2


@dataclass(frozen=True)
class RasterizedGrid:
    """One float32 (rows, columns) grid, NaN holes, CRS WKT and pixel transform."""

    array: np.ndarray
    crs_wkt: str
    transform: Affine


@dataclass
class _RasterState:
    """Mutable aggregation state for one requested raster product."""

    bounds: tuple[float, float, float, float]
    width: int
    height: int
    buffer: np.ndarray


class LidarRasterizer:
    """Chunk-read point clouds; aggregate DSM maxima or ground-class DTM minima.

    Two point passes are intentional: the first measures bounds after class
    filtering, the second fills that grid without loading every point into RAM.
    The output grid itself remains in memory; point_chunk_size only bounds the
    point buffers. resolution uses the horizontal CRS units (metres or feet).
    """
    def __init__(
        self,
        resolution: float = 1.0,
        exclude_classes: list[int] | None = None,
        point_chunk_size: int = 2_000_000,
    ):
        if resolution <= 0:
            raise ValueError('resolution must be > 0')
        if point_chunk_size < 1:
            raise ValueError('point_chunk_size must be >= 1')
        self.resolution = resolution
        self.exclude_classes = tuple(sorted(set([7, 18] if exclude_classes is None else exclude_classes)))
        self.point_chunk_size = point_chunk_size

    def rasterize(
        self,
        path: str,
        products: list[str],
        fallback_crs: str | CRS | None = None,
    ) -> dict[str, RasterizedGrid]:
        """Return e.g. {'dsm': RasterizedGrid(...)}; this method writes no files.

        Width/height round upward at the requested resolution. The resulting
        right/bottom edge may extend less than one pixel past the filtered bounds.
        Empty cells are NaN; an entirely empty requested product is an error.
        A CRS declared by the point-cloud header always wins. fallback_crs is
        only for reviewed projects whose complete LAZ header declares no CRS.
        """
        requested = list(dict.fromkeys(product for product in products if product in ('dsm', 'dtm')))
        if not requested:
            return {}

        bounds = {
            product: [np.inf, np.inf, -np.inf, -np.inf]
            for product in requested
        }
        with laspy.open(path) as reader:
            crs = reader.header.parse_crs()
            if crs is None and fallback_crs is not None:
                crs = CRS.from_user_input(fallback_crs)
            if crs is None:
                raise RuntimeError(f'No CRS found in LAZ header: {reader.header.file_source_id}')
            for points in reader.chunk_iterator(self.point_chunk_size):
                x = np.asarray(points.x)
                y = np.asarray(points.y)
                classifications = np.asarray(points.classification)
                for product in requested:
                    mask = self._classification_mask(product, classifications)
                    if not np.any(mask):
                        continue
                    product_x = x[mask]
                    product_y = y[mask]
                    item = bounds[product]
                    item[0] = min(item[0], float(product_x.min()))
                    item[1] = min(item[1], float(product_y.min()))
                    item[2] = max(item[2], float(product_x.max()))
                    item[3] = max(item[3], float(product_y.max()))

        states: dict[str, _RasterState] = {}
        for product, (xmin, ymin, xmax, ymax) in bounds.items():
            if not np.isfinite((xmin, ymin, xmax, ymax)).all():
                raise ValueError(f'No points remain after classification filtering for {product}')
            width = max(1, int(np.ceil((xmax - xmin) / self.resolution)))
            height = max(1, int(np.ceil((ymax - ymin) / self.resolution)))
            fill = -np.inf if product == 'dsm' else np.inf
            states[product] = _RasterState(
                bounds=(xmin, ymin, xmax, ymax),
                width=width,
                height=height,
                buffer=np.full(height * width, fill, dtype=np.float32),
            )

        with laspy.open(path) as reader:
            for points in reader.chunk_iterator(self.point_chunk_size):
                x = np.asarray(points.x)
                y = np.asarray(points.y)
                z = np.asarray(points.z)
                classifications = np.asarray(points.classification)
                for product, state in states.items():
                    mask = self._classification_mask(product, classifications)
                    if not np.any(mask):
                        continue
                    xmin, _, _, ymax = state.bounds
                    columns = np.clip(
                        ((x[mask] - xmin) / self.resolution).astype(int),
                        0,
                        state.width - 1,
                    )
                    rows = np.clip(
                        ((ymax - y[mask]) / self.resolution).astype(int),
                        0,
                        state.height - 1,
                    )
                    indexes = rows * state.width + columns
                    aggregate = np.maximum.at if product == 'dsm' else np.minimum.at
                    aggregate(state.buffer, indexes, z[mask])

        result: dict[str, RasterizedGrid] = {}
        for product, state in states.items():
            state.buffer[np.isinf(state.buffer)] = np.nan
            array = state.buffer.reshape(state.height, state.width)
            # Use the SAME spacing as the point-to-cell calculation above.
            # E.g. a 2.4-unit extent at resolution=1 needs 3 one-unit pixels,
            # not 3 pixels squeezed back into 2.4 units. Single-point grids
            # also retain a non-degenerate one-pixel transform.
            xmin, _, _, ymax = state.bounds
            transform = from_origin(xmin, ymax, self.resolution, self.resolution)
            result[product] = RasterizedGrid(array, crs.to_wkt(), transform)
        return result

    def _classification_mask(self, product: str, classifications: np.ndarray) -> np.ndarray:
        if product == 'dsm':
            return ~np.isin(classifications, self.exclude_classes)
        return classifications == GROUND_CLASS

    @staticmethod
    def save(path: str, product: RasterizedGrid) -> None:
        # A final filename means a closed, complete raster, including on restart.
        temporary = path + '.part'
        try:
            with rasterio.open(
                temporary,
                'w',
                driver='GTiff',
                height=product.array.shape[0],
                width=product.array.shape[1],
                count=1,
                dtype=product.array.dtype,
                crs=product.crs_wkt,
                transform=product.transform,
                nodata=np.nan,
                compress='deflate',
            ) as dataset:
                dataset.write(product.array, 1)
            os.replace(temporary, path)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)
