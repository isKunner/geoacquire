#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Crop metre-sized point windows directly on a source raster's native grid."""

import math
import os
from collections.abc import Iterable

import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from geoacquire.core.context import RuntimeContext
from geoacquire.core.geo import horizontal_crs
from geoacquire.core.models import (
    AcquisitionReport,
    AssetKind,
    AssetSpec,
    AssetStatus,
    Failure,
    LocalAsset,
    Region,
    validate_path_component,
    validate_product_type,
)

from .base import BasePostprocessor


class NativePointWindowPostprocessor(BasePostprocessor):
    """Write a point-centred metric window without warping source elevations.

    The requested centre is snapped to the nearest source pixel-grid position.
    Pixels are copied directly from aligned source rasters. An explicitly enabled
    fallback may resample only still-missing pixels from a nearly identical source
    grid. Geographic source rasters use a local geodesic estimate to choose the
    closest integer number of native pixels.
    """

    def __init__(
        self,
        size_m: float,
        input_products: list[str],
        output_dir: str | None = None,
        output_suffix: str | None = None,
        min_valid_fraction: float = 1.0,
        max_grid_snap_pixels: float = 0.0,
        skip_existing: bool = True,
        fallback_resampling: str | None = None,
        max_resolution_mismatch_fraction: float = 0.001,
        max_resampled_fraction: float = 0.25,
    ):
        if not math.isfinite(size_m) or size_m <= 0:
            raise ValueError('size_m must be a finite value greater than zero')
        if not input_products or any(not str(value).strip() for value in input_products):
            raise ValueError('input_products must be a non-empty list')
        for product in input_products:
            validate_product_type(product, 'input product')
        if output_dir is not None and not output_dir:
            raise ValueError('output_dir must be null or a non-empty path')
        if output_suffix is not None:
            validate_path_component(output_suffix, 'output_suffix')
        if not 0.0 <= min_valid_fraction <= 1.0:
            raise ValueError('min_valid_fraction must be between 0 and 1')
        if not math.isfinite(max_grid_snap_pixels) or not 0.0 <= max_grid_snap_pixels <= 0.5:
            raise ValueError('max_grid_snap_pixels must be between 0 and 0.5')
        normalized_resampling = (
            None if fallback_resampling is None else str(fallback_resampling).strip().lower()
        )
        if normalized_resampling not in (None, 'bilinear'):
            raise ValueError("fallback_resampling must be null or 'bilinear'")
        if (
            not math.isfinite(max_resolution_mismatch_fraction)
            or not 0.0 <= max_resolution_mismatch_fraction <= 1.0
        ):
            raise ValueError('max_resolution_mismatch_fraction must be between 0 and 1')
        if not math.isfinite(max_resampled_fraction) or not 0.0 <= max_resampled_fraction <= 1.0:
            raise ValueError('max_resampled_fraction must be between 0 and 1')
        self.size_m = float(size_m)
        self.input_products = frozenset(input_products)
        self.output_dir = output_dir
        self.output_suffix = output_suffix
        self.min_valid_fraction = float(min_valid_fraction)
        self.max_grid_snap_pixels = float(max_grid_snap_pixels)
        self.skip_existing = skip_existing
        self.fallback_resampling = normalized_resampling
        self.max_resolution_mismatch_fraction = float(max_resolution_mismatch_fraction)
        self.max_resampled_fraction = float(max_resampled_fraction)

    def validate_asset_flow(
        self,
        available: frozenset[AssetSpec] | None,
    ) -> frozenset[AssetSpec] | None:
        if available is None:
            return None
        selected = {
            item for item in available
            if item.kind is AssetKind.RASTER and item.product in self.input_products
        }
        if not selected:
            actual = sorted(f'{item.kind}:{item.product}' for item in available) or ['none']
            raise ValueError(
                f'NativePointWindow input_products={sorted(self.input_products)} cannot '
                f'select a raster from upstream assets={actual}'
            )
        products = {item.product for item in selected}
        if len(products) != 1:
            raise ValueError(f'NativePointWindow requires one product, got: {sorted(products)}')
        return available

    def process(
        self,
        regions: dict[str, Region],
        report: AcquisitionReport,
        context: RuntimeContext,
    ) -> AcquisitionReport:
        for region_id, result in report.regions.items():
            assets = sorted(
                (
                    asset for asset in result.usable()
                    if asset.kind is AssetKind.RASTER and asset.product in self.input_products
                ),
                key=lambda asset: asset.path,
            )
            if not assets:
                continue
            region = regions[region_id]
            destination = self._destination(region_id, assets, context)
            output_asset = LocalAsset(
                region_id=region_id,
                asset_id=f'{region_id}:native_point_window',
                path=destination,
                kind=AssetKind.RASTER,
                product=self._one_product(asset.product for asset in assets),
                metadata={
                    'requested_size_m': self.size_m,
                    'point': region.point,
                    'point_crs': region.point_crs,
                },
            )
            if self.skip_existing and os.path.isfile(destination):
                report.add_asset(output_asset, AssetStatus.SKIPPED)
                continue
            try:
                metadata = self._write_native_crop(region, assets, destination)
                report.add_asset(
                    LocalAsset(
                        region_id=output_asset.region_id,
                        asset_id=output_asset.asset_id,
                        path=output_asset.path,
                        kind=output_asset.kind,
                        product=output_asset.product,
                        metadata={**output_asset.metadata, **metadata},
                    ),
                    AssetStatus.SUCCESS,
                )
            except Exception as exc:
                report.add_failure(Failure(region_id, output_asset.asset_id, str(exc)))
        return report

    def expected_output(
        self,
        region: Region,
        context: RuntimeContext,
        default_output_dir: str | None,
    ) -> LocalAsset | None:
        if not self.skip_existing:
            return None
        output_dir = (
            context.resolve_path(self.output_dir)
            if self.output_dir is not None
            else default_output_dir
        )
        if output_dir is None:
            return None
        return LocalAsset(
            region_id=region.region_id,
            asset_id=f'{region.region_id}:native_point_window',
            path=os.path.join(output_dir, self._filename(region.region_id)),
            kind=AssetKind.RASTER,
            product=self._one_product(self.input_products),
            metadata={
                'requested_size_m': self.size_m,
                'point': region.point,
                'point_crs': region.point_crs,
            },
        )

    def _write_native_crop(
        self,
        region: Region,
        assets: list[LocalAsset],
        destination: str,
    ) -> dict[str, object]:
        if region.point is None or region.point_crs is None:
            raise ValueError('Native point crop requires point coordinates and point_crs metadata')
        anchor_path = self._select_anchor(region, assets)
        with rasterio.open(anchor_path) as anchor:
            if anchor.crs is None:
                raise ValueError(f'No CRS found in source raster: {anchor_path}')
            point_x, point_y = Transformer.from_crs(
                region.point_crs,
                horizontal_crs(anchor.crs),
                always_xy=True,
            ).transform(*region.point)
            x_res_m, y_res_m = self._pixel_size_metres(anchor, point_x, point_y)
            width = max(1, round(self.size_m / x_res_m))
            height = max(1, round(self.size_m / y_res_m))
            point_column, point_row = (~anchor.transform) * (point_x, point_y)
            column_start = round(point_column - width / 2.0)
            row_start = round(point_row - height / 2.0)
            output_transform = anchor.transform * Affine.translation(column_start, row_start)
            actual_center = output_transform * (width / 2.0, height / 2.0)
            actual_size = (width * x_res_m, height * y_res_m)
            dtype = np.dtype(anchor.dtypes[0])
            count = anchor.count
            nodata = anchor.nodata
            crs = anchor.crs
            linear = self._linear_transform(anchor.transform)

        fill_value = self._fill_value(dtype, nodata)
        output = np.full((count, height, width), fill_value, dtype=dtype)
        valid = np.zeros((height, width), dtype=bool)
        incompatible: list[str] = []
        grid_snaps: dict[str, tuple[float, float]] = {}
        fallback_candidates: list[tuple[str, float]] = []
        for asset in [next(item for item in assets if item.path == anchor_path), *(
            item for item in assets if item.path != anchor_path
        )]:
            with rasterio.open(asset.path) as source:
                if source.crs is None or horizontal_crs(source.crs) != horizontal_crs(crs):
                    incompatible.append(f'{os.path.basename(asset.path)}: CRS')
                    continue
                if source.count != count or np.dtype(source.dtypes[0]) != dtype:
                    incompatible.append(f'{os.path.basename(asset.path)}: bands/dtype')
                    continue
                source_linear = self._linear_transform(source.transform)
                if not self._same_linear_grid(linear, source_linear):
                    mismatch = self._resolution_mismatch_fraction(linear, source_linear)
                    if (
                        self.fallback_resampling is not None
                        and mismatch is not None
                        and mismatch <= self.max_resolution_mismatch_fraction + 1e-12
                    ):
                        fallback_candidates.append((asset.path, mismatch))
                    else:
                        incompatible.append(f'{os.path.basename(asset.path)}: resolution/rotation')
                    continue
                source_column, source_row = (~source.transform) * (
                    output_transform.c,
                    output_transform.f,
                )
                rounded_column = round(source_column)
                rounded_row = round(source_row)
                grid_snap = (
                    source_column - rounded_column,
                    source_row - rounded_row,
                )
                if max(abs(value) for value in grid_snap) > self.max_grid_snap_pixels + 1e-9:
                    incompatible.append(f'{os.path.basename(asset.path)}: grid origin')
                    continue
                if max(abs(value) for value in grid_snap) > 1e-9:
                    grid_snaps[os.path.basename(asset.path)] = grid_snap
                self._copy_overlap(
                    source,
                    rounded_column,
                    rounded_row,
                    output,
                    valid,
                )
            if valid.all():
                break

        resampled_pixels = 0
        resampled_sources: list[str] = []
        used_mismatches: list[float] = []
        if (
            float(valid.mean()) + 1e-12 < self.min_valid_fraction
            and self.fallback_resampling is not None
            and fallback_candidates
        ):
            resampled_pixels, resampled_sources, used_mismatches = self._fill_resampled_gaps(
                fallback_candidates,
                crs,
                output_transform,
                output,
                valid,
                fill_value,
                incompatible,
            )

        valid_fraction = float(valid.mean())
        if valid_fraction + 1e-12 < self.min_valid_fraction:
            detail = f'; incompatible={incompatible}' if incompatible else ''
            raise ValueError(
                f'Native crop valid coverage is {valid_fraction:.6f}, below '
                f'min_valid_fraction={self.min_valid_fraction:.6f}{detail}'
            )

        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        stem, extension = os.path.splitext(destination)
        temporary = f'{stem}.part{extension}'
        if os.path.isfile(temporary):
            os.remove(temporary)
        profile = {
            'driver': 'GTiff',
            'width': width,
            'height': height,
            'count': count,
            'dtype': dtype.name,
            'crs': crs,
            'transform': output_transform,
            'nodata': nodata,
            'compress': 'deflate',
            'tiled': True,
            'blockxsize': 256,
            'blockysize': 256,
            'BIGTIFF': 'IF_SAFER',
        }
        try:
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK='YES'):
                with rasterio.open(temporary, 'w', **profile) as target:
                    target.write(output)
                    target.write_mask(valid.astype(np.uint8) * 255)
                    target.update_tags(
                        GEOACQUIRE_POINT_X=str(region.point[0]),
                        GEOACQUIRE_POINT_Y=str(region.point[1]),
                        GEOACQUIRE_POINT_CRS=region.point_crs,
                        GEOACQUIRE_REQUESTED_SIZE_M=str(self.size_m),
                        GEOACQUIRE_ACTUAL_WIDTH_M=str(actual_size[0]),
                        GEOACQUIRE_ACTUAL_HEIGHT_M=str(actual_size[1]),
                        GEOACQUIRE_CENTER_OFFSET_X=str(actual_center[0] - point_x),
                        GEOACQUIRE_CENTER_OFFSET_Y=str(actual_center[1] - point_y),
                        GEOACQUIRE_RESAMPLING=(
                            'none'
                            if resampled_pixels == 0
                            else f'partial_{self.fallback_resampling}'
                        ),
                        GEOACQUIRE_RESAMPLED_PIXELS=str(resampled_pixels),
                        GEOACQUIRE_RESAMPLED_FRACTION=str(resampled_pixels / valid.size),
                        GEOACQUIRE_RESAMPLED_SOURCES=';'.join(resampled_sources),
                        GEOACQUIRE_MAX_RESOLUTION_MISMATCH_FRACTION=str(
                            max(used_mismatches, default=0.0)
                        ),
                        GEOACQUIRE_MAX_GRID_SNAP_PIXELS=str(
                            max(
                                (abs(value) for snap in grid_snaps.values() for value in snap),
                                default=0.0,
                            )
                        ),
                        GEOACQUIRE_SOURCE_COUNT=str(len(assets)),
                        GEOACQUIRE_VALID_FRACTION=str(valid_fraction),
                    )
            os.replace(temporary, destination)
        except Exception:
            if os.path.isfile(temporary):
                os.remove(temporary)
            raise
        return {
            'source_count': len(assets),
            'source_crs': str(crs),
            'native_resolution_m': (x_res_m, y_res_m),
            'actual_size_m': actual_size,
            'actual_center': actual_center,
            'center_offset': (actual_center[0] - point_x, actual_center[1] - point_y),
            'valid_fraction': valid_fraction,
            'grid_snaps_pixels': grid_snaps,
            'resampling': (
                'none'
                if resampled_pixels == 0
                else f'partial_{self.fallback_resampling}'
            ),
            'resampled_pixels': resampled_pixels,
            'resampled_fraction': resampled_pixels / valid.size,
            'resampled_sources': resampled_sources,
            'max_resolution_mismatch_fraction': max(used_mismatches, default=0.0),
        }

    def _fill_resampled_gaps(
        self,
        candidates: list[tuple[str, float]],
        crs: object,
        output_transform: Affine,
        output: np.ndarray,
        valid: np.ndarray,
        fill_value: object,
        incompatible: list[str],
    ) -> tuple[int, list[str], list[float]]:
        """Fill only missing target pixels from narrowly compatible source grids."""
        resampling = Resampling.bilinear
        height, width = valid.shape
        count = output.shape[0]
        resampled_pixels = 0
        resampled_sources: list[str] = []
        used_mismatches: list[float] = []
        maximum_pixels = self.max_resampled_fraction * valid.size

        for path, mismatch in candidates:
            if float(valid.mean()) + 1e-12 >= self.min_valid_fraction:
                break
            basename = os.path.basename(path)
            with rasterio.open(path) as source:
                vrt_options = {
                    'crs': crs,
                    'transform': output_transform,
                    'width': width,
                    'height': height,
                    'resampling': resampling,
                }
                if source.nodata is None:
                    vrt_options['add_alpha'] = True
                else:
                    vrt_options['src_nodata'] = source.nodata
                    vrt_options['nodata'] = fill_value
                with WarpedVRT(source, **vrt_options) as warped:
                    values = warped.read(indexes=list(range(1, count + 1)), masked=True)
                    source_valid = warped.dataset_mask() > 0

            source_valid &= ~np.ma.getmaskarray(values).any(axis=0)
            take = source_valid & ~valid
            new_pixels = int(np.count_nonzero(take))
            if new_pixels == 0:
                continue
            if resampled_pixels + new_pixels > maximum_pixels + 1e-9:
                fraction = (resampled_pixels + new_pixels) / valid.size
                incompatible.append(
                    f'{basename}: resampled fraction {fraction:.6f} exceeds '
                    f'{self.max_resampled_fraction:.6f}'
                )
                continue
            output[:, take] = np.asarray(values.data)[:, take]
            valid[take] = True
            resampled_pixels += new_pixels
            resampled_sources.append(basename)
            used_mismatches.append(mismatch)

        return resampled_pixels, resampled_sources, used_mismatches

    def _select_anchor(self, region: Region, assets: list[LocalAsset]) -> str:
        candidates: list[tuple[float, str]] = []
        failures: list[str] = []
        for asset in assets:
            try:
                with rasterio.open(asset.path) as source:
                    if source.crs is None:
                        failures.append(f'{os.path.basename(asset.path)}: no CRS')
                        continue
                    point = Transformer.from_crs(
                        region.point_crs,
                        horizontal_crs(source.crs),
                        always_xy=True,
                    ).transform(*region.point)
                    column, row = (~source.transform) * point
                    if 0.0 <= column <= source.width and 0.0 <= row <= source.height:
                        margin = min(column, source.width - column, row, source.height - row)
                        candidates.append((margin, asset.path))
            except Exception as exc:
                failures.append(f'{os.path.basename(asset.path)}: {exc}')
        if not candidates:
            raise ValueError(
                f'No downloaded source raster contains point {region.point}; details={failures}'
            )
        return max(candidates, key=lambda item: (item[0], item[1]))[1]

    @staticmethod
    def _copy_overlap(source, offset_column, offset_row, output, valid) -> None:
        height, width = valid.shape
        source_column = max(0, offset_column)
        source_row = max(0, offset_row)
        target_column = max(0, -offset_column)
        target_row = max(0, -offset_row)
        copy_width = min(width - target_column, source.width - source_column)
        copy_height = min(height - target_row, source.height - source_row)
        if copy_width <= 0 or copy_height <= 0:
            return
        window = Window(source_column, source_row, copy_width, copy_height)
        values = source.read(window=window, masked=True)
        source_valid = ~np.ma.getmaskarray(values).any(axis=0)
        destination_valid = valid[
            target_row:target_row + copy_height,
            target_column:target_column + copy_width,
        ]
        take = source_valid & ~destination_valid
        if not np.any(take):
            return
        destination = output[
            :,
            target_row:target_row + copy_height,
            target_column:target_column + copy_width,
        ]
        destination[:, take] = np.asarray(values.data)[:, take]
        destination_valid[take] = True

    @staticmethod
    def _pixel_size_metres(source, point_x: float, point_y: float) -> tuple[float, float]:
        crs = horizontal_crs(source.crs)
        transform = source.transform
        if crs.is_projected:
            axes = crs.axis_info
            x_factor = float(axes[0].unit_conversion_factor)
            y_factor = float(axes[1].unit_conversion_factor)
            return (
                math.hypot(transform.a * x_factor, transform.d * y_factor),
                math.hypot(transform.b * x_factor, transform.e * y_factor),
            )
        column, row = (~transform) * (point_x, point_y)
        origin = transform * (column, row)
        x_next = transform * (column + 1.0, row)
        y_next = transform * (column, row + 1.0)
        to_wgs84 = Transformer.from_crs(crs, 'EPSG:4326', always_xy=True)
        origin_ll = to_wgs84.transform(*origin)
        x_ll = to_wgs84.transform(*x_next)
        y_ll = to_wgs84.transform(*y_next)
        geod = CRS.from_epsg(4326).get_geod()
        x_distance = abs(geod.inv(*origin_ll, *x_ll)[2])
        y_distance = abs(geod.inv(*origin_ll, *y_ll)[2])
        if x_distance <= 0 or y_distance <= 0:
            raise ValueError(f'Cannot estimate geographic source pixel size: {source.name}')
        return x_distance, y_distance

    def _destination(
        self,
        region_id: str,
        assets: list[LocalAsset],
        context: RuntimeContext,
    ) -> str:
        output_dir = (
            os.path.dirname(os.path.abspath(assets[0].path))
            if self.output_dir is None
            else context.resolve_path(self.output_dir)
        )
        return os.path.join(output_dir, self._filename(region_id))

    def _filename(self, region_id: str) -> str:
        suffix = '' if self.output_suffix is None else f'_{self.output_suffix}'
        return f'{region_id}{suffix}.tif'

    @staticmethod
    def _linear_transform(transform: Affine) -> tuple[float, float, float, float]:
        return transform.a, transform.b, transform.d, transform.e

    @staticmethod
    def _same_linear_grid(left, right) -> bool:
        scale = max(1.0, *(abs(value) for value in left), *(abs(value) for value in right))
        return all(abs(a - b) <= scale * 1e-9 for a, b in zip(left, right))

    @staticmethod
    def _resolution_mismatch_fraction(left, right) -> float | None:
        """Return scale mismatch for equal-orientation grids, otherwise ``None``."""
        left_vectors = ((left[0], left[2]), (left[1], left[3]))
        right_vectors = ((right[0], right[2]), (right[1], right[3]))
        mismatches: list[float] = []
        for left_vector, right_vector in zip(left_vectors, right_vectors):
            left_length = math.hypot(*left_vector)
            right_length = math.hypot(*right_vector)
            if left_length <= 0.0 or right_length <= 0.0:
                return None
            left_unit = tuple(value / left_length for value in left_vector)
            right_unit = tuple(value / right_length for value in right_vector)
            if any(abs(a - b) > 1e-9 for a, b in zip(left_unit, right_unit)):
                return None
            mismatches.append(abs(right_length - left_length) / left_length)
        return max(mismatches)

    @staticmethod
    def _fill_value(dtype: np.dtype, nodata: float | None):
        if nodata is not None:
            return nodata
        if np.issubdtype(dtype, np.floating):
            return np.nan
        return 0

    @staticmethod
    def _one_product(products: Iterable[str]) -> str:
        values = sorted(set(products))
        if len(values) != 1:
            raise ValueError(f'NativePointWindow requires one product, got: {values}')
        return values[0]
