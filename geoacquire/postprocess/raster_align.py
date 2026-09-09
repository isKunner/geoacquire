#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: raster_align.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Windowed raster mosaic, crop, reprojection, and alignment to a target raster grid.

import math
import os
from collections.abc import Iterable
from threading import Lock

import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS as PyprojCRS
from pyproj.crs import CompoundCRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds, transform as window_transform

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
    RegionMetadataKey,
    validate_product_type,
    validate_path_component,
)

from .base import BasePostprocessor


class RasterAlignToTargetPostprocessor(BasePostprocessor):
    """Mosaic report rasters and write one target-oriented GeoTIFF per Region.

    The processor consumes only LocalAssets already present in the current
    pipeline report. It never scans the Region output directory, so files from
    another Source cannot be mixed into the mosaic accidentally.

    `target_scale=1` writes the exact target raster grid. A value greater than
    one keeps the same bounds and upper-left grid origin while multiplying the
    target pixel size. Target width and height must be divisible by the scale.

    Example: Region '0', shape=(1024,1024), scale=32 -> 0_cop.tif with
    shape=(32,32), the same extent and a 32-times larger pixel size. Input
    assets are filtered first by AssetKind.RASTER, then by the case-sensitive
    LocalAsset.product whitelist in input_products, never by filename.
    """

    def __init__(
        self,
        output_suffix: str,
        input_products: list[str] | None = None,
        output_dir: str | None = None,
        resample: bool = True,
        target_scale: int = 1,
        resampling: str = 'bilinear',
        merge_method: str = 'first',
        window_size: int = 1024,
        skip_existing: bool = True,
        vertical_datum_policy: str = 'ignore',
        skip_without_target: bool = True,
    ):
        """
        Initialize a target-aligned raster post-processor.

        Args:
            output_suffix:
                Suffix appended to the Region ID to form the output filename.
                The final file is always named ``<region_id>_<output_suffix>.tif``.
                Must be a non-empty path component without directory separators.
            input_products:
                ``list[str] | None`` whitelist of case-sensitive product types
                stored in ``LocalAsset.product``. A non-empty list includes matching
                raster assets; ``None`` includes every ``kind='raster'`` asset in
                the current pipeline report; an empty list is invalid. Built-in
                acquisition products from ``ProductType`` are ``dsm`` and ``dtm`` for USGS LiDAR,
                ``dem`` for USGS/LINZ/Copernicus DEM, and ``imagery`` for
                Google/Wayback. ``laz`` is a point-cloud product and cannot enter
                this raster processor. Custom Sources may define other non-empty
                products. One processor invocation must resolve to exactly one
                product type; its output inherits that product.
            output_dir:
                Directory where the output GeoTIFF is written. If ``None``, the
                processor places the result in the same directory as the first input
                raster asset (the "follow acquire" behaviour). If a path is given,
                results go directly into that directory without creating Region
                subdirectories.
            resample:
                If ``True``, the output grid exactly matches the target raster's
                transform and shape (subject to ``target_scale``). If ``False``, the
                output preserves the source resolution and only crops to the target
                bounds; in this case ``target_scale`` must be ``1``.
            target_scale:
                Spatial decimation factor relative to the target grid. ``1`` writes
                the exact target grid. A value greater than ``1`` keeps the same
                geographic bounds and upper-left origin while multiplying the pixel
                size by this factor. The target width and height must both be
                evenly divisible by ``target_scale``. Example: a target of
                ``(1024, 1024)`` with ``target_scale=32`` produces a
                ``(32, 32)`` output.
            resampling:
                Resampling kernel used when warping or scaling source rasters onto
                the output grid. Supported names correspond to
                ``rasterio.enums.Resampling``: ``nearest``, ``bilinear``,
                ``cubic``, ``cubic_spline``, ``lanczos``, ``average``, ``mode``,
                ``max``, ``min``, ``med``, ``q1``, ``q3``, ``sum``, ``rms``.
                Defaults to ``'bilinear'``.
            merge_method:
                Overlap strategy when multiple source rasters cover the same pixel.
                ``'first'`` keeps the value from the first source that touches the
                pixel; ``'last'`` overwrites with the last source in the sorted
                asset list.
            window_size:
                Pixel dimension of the internal read/write window. The output is
                processed in square tiles of this size to keep memory bounded.
                Must be >= 1.
            skip_existing:
                If ``True`` and the destination file already exists, the step is
                skipped and a ``skipped`` status asset is recorded instead of
                rewriting the file.
            vertical_datum_policy:
                Policy for mismatched vertical CRS between source and target.
                ``'ignore'`` silently proceeds without elevation conversion.
                ``'warn_once'`` prints a single summary warning per processor
                instance but still does not convert. No actual vertical
                transformation is ever performed.
            skip_without_target:
                If ``True`` and a Region lacks ``target_path`` metadata (e.g. a
                pure bbox input), the Region is silently skipped. If ``False``, a
                ``Failure`` is recorded in the report for that Region.

        Raises:
            ValueError: If ``output_suffix`` is not a valid path component;
                if ``input_products`` is empty or contains an empty product; if
                ``target_scale < 1``; if ``resample`` is ``False`` and
                ``target_scale != 1``; if ``resampling`` is not a recognized
                method; if ``merge_method`` is not ``'first'`` or ``'last'``;
                if ``window_size < 1``; or if ``vertical_datum_policy`` is not
                ``'ignore'`` or ``'warn_once'``.
        """
        validate_path_component(output_suffix, 'output_suffix')
        if input_products is not None and (
            not input_products
            or any(not isinstance(product, str) or not product.strip() for product in input_products)
        ):
            raise ValueError('input_products must be null or a non-empty list of non-empty strings')
        if input_products is not None:
            for product in input_products:
                validate_product_type(product, 'input product')
        if target_scale < 1:
            raise ValueError('target_scale must be >= 1')
        if not resample and target_scale != 1:
            raise ValueError('target_scale must be 1 when resample is false')
        try:
            resampling_method = Resampling[resampling]
        except KeyError as exc:
            choices = ', '.join(item.name for item in Resampling)
            raise ValueError(f'Unsupported resampling method {resampling!r}; choose from: {choices}') from exc
        if merge_method not in ('first', 'last'):
            raise ValueError("merge_method must be 'first' or 'last'")
        if window_size < 1:
            raise ValueError('window_size must be >= 1')
        if vertical_datum_policy not in ('ignore', 'warn_once'):
            raise ValueError("vertical_datum_policy must be 'ignore' or 'warn_once'")

        self.output_suffix = output_suffix
        self.input_products = set(input_products) if input_products is not None else None
        self.output_dir = output_dir
        self.resample = resample
        self.target_scale = target_scale
        self.resampling_name = resampling
        self.resampling = resampling_method
        self.merge_method = merge_method
        self.window_size = window_size
        self.skip_existing = skip_existing
        self.vertical_datum_policy = vertical_datum_policy
        self.skip_without_target = skip_without_target
        self._vertical_warning_emitted = False
        self._vertical_warning_lock = Lock()

    def validate_asset_flow(
        self,
        available: frozenset[AssetSpec] | None,
    ) -> frozenset[AssetSpec] | None:
        """Reject a known pipeline contract that cannot provide selected rasters."""
        if available is None:
            return None
        selected = {
            spec
            for spec in available
            if spec.kind is AssetKind.RASTER
            and (self.input_products is None or spec.product in self.input_products)
        }
        if not selected:
            expected = 'any raster product' if self.input_products is None else sorted(self.input_products)
            actual = sorted(f'{spec.kind}:{spec.product}' for spec in available) or ['none']
            raise ValueError(
                f'RasterAlign input_products={expected} cannot select a raster from '
                f'upstream assets={actual}'
            )
        self._require_one_product(spec.product for spec in selected)
        # Alignment changes the raster grid and filename, not what the data is.
        # The output therefore has the same kind/product contract as its input.
        return available

    def process(
        self,
        regions: dict[str, Region],
        report: AcquisitionReport,
        context: RuntimeContext,
    ) -> AcquisitionReport:
        candidates = {
            region_id: self._raster_assets(result.usable())
            for region_id, result in report.regions.items()
        }
        # Several Regions can enter this shared processor concurrently. Only the
        # warn-once state needs protection; all raster I/O below is per-call.
        with self._vertical_warning_lock:
            self._warn_vertical_datum_once(regions, candidates, context)

        for region_id, assets in candidates.items():
            if not assets:
                continue
            region = regions[region_id]
            if not region.target_path:
                message = 'No target raster metadata is available; target alignment was skipped'
                if self.skip_without_target:
                    if context.reporting is None:
                        print(f'[postprocess/align] region={region_id} skipped: {message.lower()}')
                else:
                    report.add_failure(Failure(region_id, f'{region_id}:{self.output_suffix}', message))
                continue

            destination = self._destination(region_id, assets, context)
            output_asset = self._output_asset(region_id, destination, assets, region)
            destination_exists = False
            if self.skip_existing:
                destination_exists = (
                    os.path.normcase(os.path.basename(destination))
                    in context.reporting.scan_filenames(os.path.dirname(os.path.abspath(destination)))
                    if context.reporting is not None
                    else os.path.isfile(destination)
                )
            if self.skip_existing and destination_exists:
                report.add_asset(output_asset, AssetStatus.SKIPPED)
                if context.reporting is None:
                    print(f'[postprocess/align] region={region_id} skipped existing={destination}')
                continue

            try:
                self._write_aligned_raster(region, assets, destination)
                report.add_asset(output_asset, AssetStatus.SUCCESS)
                if context.reporting is None:
                    print(
                        f'[postprocess/align] region={region_id} sources={len(assets)} '
                        f'output={destination}'
                    )
            except Exception as exc:
                report.add_failure(Failure(region_id, f'{region_id}:{self.output_suffix}', str(exc)))
                if context.reporting is None:
                    print(f'[postprocess/align] region={region_id} failed: {exc}')
        return report

    def expected_output(
        self,
        region: Region,
        context: RuntimeContext,
        default_output_dir: str | None,
    ) -> LocalAsset | None:
        """Describe an inferable aligned result without opening inputs or disk."""
        if (
            not self.skip_existing
            or not region.target_path
            or (self.output_dir is None and default_output_dir is None)
            or self.input_products is None
            or len(self.input_products) != 1
        ):
            return None
        output_dir = (
            default_output_dir
            if self.output_dir is None
            else context.resolve_path(self.output_dir)
        )
        product = next(iter(self.input_products))
        destination = os.path.join(output_dir, f'{region.region_id}_{self.output_suffix}.tif')
        return LocalAsset(
            region_id=region.region_id,
            asset_id=f'{region.region_id}:{self.output_suffix}',
            path=destination,
            kind=AssetKind.RASTER,
            product=product,
            metadata={
                RegionMetadataKey.TARGET_PATH: region.target_path,
                'resample': self.resample,
                'target_scale': self.target_scale,
                'resampling': self.resampling_name,
                'vertical_conversion': False,
            },
        )

    def _raster_assets(self, assets: Iterable[LocalAsset]) -> list[LocalAsset]:
        selected = [
            asset
            for asset in assets
            if asset.kind is AssetKind.RASTER
            and (self.input_products is None or asset.product in self.input_products)
        ]
        self._require_one_product(asset.product for asset in selected)
        return sorted(selected, key=lambda asset: asset.path)

    @staticmethod
    def _require_one_product(products: Iterable[str]) -> str | None:
        """Return the selected product, rejecting mosaics with mixed meanings."""
        unique = sorted(set(products))
        if not unique:
            return None
        if len(unique) != 1:
            raise ValueError(
                'RasterAlign cannot combine multiple products in one output: '
                f'{unique}; configure one input product per processor'
            )
        return unique[0]

    def _destination(
        self,
        region_id: str,
        assets: list[LocalAsset],
        context: RuntimeContext,
    ) -> str:
        # One parameter, two behaviours: null keeps the result with the input
        # rasters; an explicit path writes directly into that directory. Naming
        # remains the independent responsibility of region_id + output_suffix.
        output_dir = (
            os.path.dirname(os.path.abspath(assets[0].path))
            if self.output_dir is None
            else context.resolve_path(self.output_dir)
        )
        return os.path.join(output_dir, f'{region_id}_{self.output_suffix}.tif')

    def _output_asset(
        self,
        region_id: str,
        destination: str,
        assets: list[LocalAsset],
        region: Region,
    ) -> LocalAsset:
        product = self._require_one_product(asset.product for asset in assets)
        if product is None:
            raise ValueError('RasterAlign cannot create an output without an input product')
        return LocalAsset(
            region_id=region_id,
            asset_id=f'{region_id}:{self.output_suffix}',
            path=destination,
            kind=AssetKind.RASTER,
            product=product,
            metadata={
                'source_count': len(assets),
                RegionMetadataKey.TARGET_PATH: region.target_path,
                'resample': self.resample,
                'target_scale': self.target_scale,
                'resampling': self.resampling_name,
                'vertical_conversion': False,
            },
        )

    def _write_aligned_raster(
        self,
        region: Region,
        assets: list[LocalAsset],
        destination: str,
    ) -> None:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        stem, extension = os.path.splitext(destination)
        temporary = f'{stem}.part{extension}'
        if os.path.isfile(temporary):
            os.remove(temporary)

        try:
            with rasterio.open(assets[0].path) as first:
                if first.crs is None:
                    raise ValueError(f'No CRS found in source raster: {assets[0].path}')
                count = first.count
                dtype = first.dtypes[0]
                nodata = first.nodata
                storage_crs, warp_crs, output_transform, width, height = self._output_grid(region, first)
                source_vertical = self._vertical_identifier(first.crs)
                reference_vertical = self._vertical_identifier(region.crs)

            profile = {
                'driver': 'GTiff',
                'width': width,
                'height': height,
                'count': count,
                'dtype': dtype,
                'crs': storage_crs,
                'transform': output_transform,
                'nodata': nodata,
                'compress': 'deflate',
                'tiled': True,
                'blockxsize': 256,
                'blockysize': 256,
                'BIGTIFF': 'IF_SAFER',
            }
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK='YES'):
                with rasterio.open(temporary, 'w+', **profile) as output:
                    self._initialize_output(output, count, dtype, nodata)
                    for asset in assets:
                        self._merge_source(output, warp_crs, asset.path, count, dtype)
                    output.update_tags(
                        GEOACQUIRE_SOURCE_COUNT=str(len(assets)),
                        GEOACQUIRE_TARGET_PATH=str(region.target_path),
                        GEOACQUIRE_TARGET_SCALE=str(self.target_scale),
                        GEOACQUIRE_RESAMPLING=self.resampling_name,
                        GEOACQUIRE_VERTICAL_DATUM_POLICY=self.vertical_datum_policy,
                        GEOACQUIRE_VERTICAL_CONVERSION='false',
                        GEOACQUIRE_SOURCE_VERTICAL_CRS=source_vertical or 'unknown',
                        GEOACQUIRE_REFERENCE_VERTICAL_CRS=reference_vertical or 'unknown',
                    )
            os.replace(temporary, destination)
        except Exception:
            if os.path.isfile(temporary):
                os.remove(temporary)
            raise

    def _output_grid(
        self,
        region: Region,
        first: rasterio.io.DatasetReader,
    ) -> tuple[object, PyprojCRS, Affine, int, int]:
        source_horizontal = self._horizontal_crs(first.crs)
        if source_horizontal is None:
            raise ValueError(f'No horizontal CRS found in source raster: {first.name}')
        if self.resample:
            if region.target_shape is None or region.target_transform is None:
                raise ValueError(f'Target raster grid metadata is incomplete: {region.target_path}')
            height, width = region.target_shape
            if height % self.target_scale or width % self.target_scale:
                raise ValueError(
                    f'Target shape {(height, width)} is not divisible by target_scale={self.target_scale}'
                )
            transform_values = region.target_transform
            target_transform = Affine(*transform_values[:6])
            target_horizontal = self._horizontal_crs(region.crs)
            if target_horizontal is None:
                raise ValueError(f'No horizontal CRS found in target raster: {region.target_path}')
            source_vertical = self._vertical_crs(first.crs)
            storage_crs: object = target_horizontal.to_wkt()
            if source_vertical is not None:
                storage_crs = CompoundCRS(
                    f'{target_horizontal.name} + {source_vertical.name}',
                    [target_horizontal, source_vertical],
                ).to_wkt()
            return (
                storage_crs,
                target_horizontal,
                target_transform * Affine.scale(self.target_scale, self.target_scale),
                width // self.target_scale,
                height // self.target_scale,
            )

        target_horizontal = self._horizontal_crs(region.crs)
        if target_horizontal is None:
            raise ValueError(f'No horizontal CRS found in target raster: {region.target_path}')
        source_bounds = transform_bounds(
            target_horizontal.to_wkt(),
            source_horizontal.to_wkt(),
            *region.bounds,
            densify_pts=21,
        )
        floating = from_bounds(*source_bounds, transform=first.transform)
        col_start = math.floor(floating.col_off)
        row_start = math.floor(floating.row_off)
        col_stop = math.ceil(floating.col_off + floating.width)
        row_stop = math.ceil(floating.row_off + floating.height)
        window = Window(col_start, row_start, col_stop - col_start, row_stop - row_start)
        return (
            first.crs,
            source_horizontal,
            window_transform(window, first.transform),
            int(window.width),
            int(window.height),
        )

    def _initialize_output(
        self,
        output: rasterio.io.DatasetWriter,
        count: int,
        dtype: str,
        nodata: float | None,
    ) -> None:
        fill_value = 0 if nodata is None else nodata
        full = Window(0, 0, output.width, output.height)
        for window in self._iter_windows(full):
            shape = (count, int(window.height), int(window.width))
            output.write(np.full(shape, fill_value, dtype=np.dtype(dtype)), window=window)
            output.write_mask(np.zeros(shape[1:], dtype=np.uint8), window=window)

    def _merge_source(
        self,
        output: rasterio.io.DatasetWriter,
        output_horizontal: PyprojCRS,
        source_path: str,
        expected_count: int,
        output_dtype: str,
    ) -> None:
        with rasterio.open(source_path) as source:
            if source.crs is None:
                raise ValueError(f'No CRS found in source raster: {source_path}')
            if source.count != expected_count:
                raise ValueError(
                    f'Raster band count mismatch: expected {expected_count}, got {source.count} in {source_path}'
                )
            source_horizontal = self._horizontal_crs(source.crs)
            if source_horizontal is None:
                raise ValueError(f'No horizontal CRS found in source raster: {source_path}')
            bounds = transform_bounds(
                source_horizontal.to_wkt(),
                output_horizontal.to_wkt(),
                *source.bounds,
                densify_pts=21,
            )
            source_window = self._clipped_window(bounds, output.transform, output.width, output.height)
            if source_window is None:
                return

            with WarpedVRT(
                source,
                src_crs=source_horizontal.to_wkt(),
                crs=output_horizontal.to_wkt(),
                transform=output.transform,
                width=output.width,
                height=output.height,
                resampling=self.resampling,
                add_alpha=source.nodata is None,
            ) as warped:
                indexes = list(range(1, expected_count + 1))
                for window in self._iter_windows(source_window):
                    source_data = warped.read(indexes=indexes, window=window, out_dtype=output_dtype)
                    source_valid = warped.dataset_mask(window=window) > 0
                    if not np.any(source_valid):
                        continue
                    existing_valid = output.dataset_mask(window=window) > 0
                    take = source_valid if self.merge_method == 'last' else source_valid & ~existing_valid
                    if np.any(take):
                        destination_data = output.read(indexes=indexes, window=window)
                        destination_data[:, take] = source_data[:, take]
                        output.write(destination_data, indexes=indexes, window=window)
                    output.write_mask(
                        ((existing_valid | source_valid).astype(np.uint8) * 255),
                        window=window,
                    )

    def _clipped_window(
        self,
        bounds: tuple[float, float, float, float],
        transform: Affine,
        width: int,
        height: int,
    ) -> Window | None:
        floating = from_bounds(*bounds, transform=transform)
        col_start = max(0, math.floor(floating.col_off))
        row_start = max(0, math.floor(floating.row_off))
        col_stop = min(width, math.ceil(floating.col_off + floating.width))
        row_stop = min(height, math.ceil(floating.row_off + floating.height))
        if col_start >= col_stop or row_start >= row_stop:
            return None
        return Window(col_start, row_start, col_stop - col_start, row_stop - row_start)

    def _iter_windows(self, window: Window) -> Iterable[Window]:
        row_stop = int(window.row_off + window.height)
        col_stop = int(window.col_off + window.width)
        for row in range(int(window.row_off), row_stop, self.window_size):
            for column in range(int(window.col_off), col_stop, self.window_size):
                yield Window(
                    column,
                    row,
                    min(self.window_size, col_stop - column),
                    min(self.window_size, row_stop - row),
                )

    def _warn_vertical_datum_once(
        self,
        regions: dict[str, Region],
        candidates: dict[str, list[LocalAsset]],
        context: RuntimeContext | None = None,
    ) -> None:
        if self.vertical_datum_policy != 'warn_once' or self._vertical_warning_emitted:
            return

        issues: list[str] = []
        asset_count = sum(len(assets) for assets in candidates.values())
        for region_id, assets in candidates.items():
            if not assets or not regions[region_id].target_path:
                continue
            target_vertical = self._vertical_identifier(regions[region_id].crs)
            try:
                with rasterio.open(assets[0].path) as source:
                    source_vertical = self._vertical_identifier(source.crs)
            except Exception:
                continue
            if source_vertical == target_vertical:
                continue
            issues.append(
                f'region={region_id} source={source_vertical or "unknown"} '
                f'target={target_vertical or "unknown"}'
            )

        if issues:
            extra = '' if len(issues) <= 3 else f'; plus {len(issues) - 3} more regions'
            message = (
                f'[postprocess/vertical] output={self.output_suffix} policy=warn_once; '
                f'conversion disabled; {"; ".join(issues[:3])}{extra}; '
                f'pipeline raster assets={asset_count}'
            )
            if context is not None and context.reporting is not None:
                context.reporting.detail('tif/vertical-warning', message)
            else:
                print(message)
            self._vertical_warning_emitted = True

    @staticmethod
    def _vertical_identifier(crs_value: object) -> str | None:
        vertical = RasterAlignToTargetPostprocessor._vertical_crs(crs_value)
        if vertical is None:
            return None
        authority = vertical.to_authority()
        return f'{vertical.name} ({authority[0]}:{authority[1]})' if authority else vertical.name

    @staticmethod
    def _vertical_crs(crs_value: object) -> PyprojCRS | None:
        if crs_value is None:
            return None
        try:
            crs = PyprojCRS.from_user_input(crs_value)
        except Exception:
            return None
        if crs.is_compound:
            return next(
                (sub_crs for sub_crs in crs.sub_crs_list if sub_crs.is_vertical),
                None,
            )
        return crs if crs.is_vertical else None

    @staticmethod
    def _horizontal_crs(crs_value: object) -> PyprojCRS | None:
        if crs_value is None:
            return None
        try:
            return horizontal_crs(crs_value)
        except Exception:
            return None
