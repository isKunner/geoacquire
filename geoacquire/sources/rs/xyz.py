#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: xyz.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Reusable HTTPSource template for XYZ remote-sensing imagery services.

import os
from abc import abstractmethod
from collections.abc import Iterable

from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import (
    AcquisitionReport,
    AcquireOptions,
    AssetKind,
    AssetSpec,
    AssetStatus,
    DownloadRequest,
    Failure,
    LocalAsset,
    ProductType,
    Region,
)
from geoacquire.core.source import HTTPSource

from .georeference import XYZGeoReferencer
from .tile_math import bounds_to_wgs84, lonlat_to_tile, tile_bounds_wgs84


class XYZSource(HTTPSource):
    """Stream z/x/y image requests and optionally georeference each downloaded tile.

    Subclasses supply build_url/build_filename only. For example the request
    z02_x000002_y000001_google.jpg becomes a product='imagery', EPSG:3857 TIF
    asset when output_format='geotiff'; output_format='image' keeps the image.
    """

    BOUNDS_EPS = 1e-12

    def __init__(
        self,
        zoom: int = 18,
        tile_ext: str = 'jpg',
        output_format: str = 'geotiff',
        keep_download: bool = True,
    ):
        if zoom < 0:
            raise ValueError('zoom must be >= 0')
        normalized = {'jpg': 'image', 'raw': 'image', 'tif': 'geotiff'}.get(output_format, output_format)
        if normalized not in ('image', 'geotiff'):
            raise ValueError("output_format must be 'image' or 'geotiff'")
        self.zoom = zoom
        self.tile_ext = tile_ext.lstrip('.')
        self.output_format = normalized
        self.keep_download = keep_download

    @property
    def output_specs(self) -> frozenset[AssetSpec]:
        kind = AssetKind.IMAGE if self.output_format == 'image' else AssetKind.RASTER
        return frozenset({AssetSpec(kind, ProductType.IMAGERY)})

    @abstractmethod
    def build_url(self, zoom: int, x: int, y: int) -> str:
        """Return the remote URL for one XYZ tile; y increases towards the south."""
        pass

    @abstractmethod
    def build_filename(self, zoom: int, x: int, y: int) -> str:
        """Return a unique flat filename containing zNN_xNNNNNN_yNNNNNN tokens."""
        pass

    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        """Yield XYZ tile transfers as the bounded downloader asks for more work."""
        for region_id, region in regions.items():
            bounds = bounds_to_wgs84(region.bounds, region.crs)
            x0, y0 = lonlat_to_tile(bounds[0], bounds[3] - self.BOUNDS_EPS, self.zoom)
            x1, y1 = lonlat_to_tile(bounds[2] - self.BOUNDS_EPS, bounds[1], self.zoom)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    filename = self.build_filename(self.zoom, x, y)
                    yield DownloadRequest(
                        region_id=region_id,
                        asset_id=f'z{self.zoom}/x{x}/y{y}',
                        url=self.build_url(self.zoom, x, y),
                        filename=filename,
                        kind=AssetKind.IMAGE,
                        product=ProductType.IMAGERY,
                        metadata={
                            'zxy': (self.zoom, x, y),
                            'bounds_wgs84': tile_bounds_wgs84(self.zoom, x, y),
                        },
                    )

    def materialize(
        self,
        report: AcquisitionReport,
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        if self.output_format == 'image':
            return report

        materialized = AcquisitionReport.for_regions(report.regions)
        materialized.copy_failures_from(report)
        for _, asset in report.iter_assets():
            destination = os.path.splitext(asset.path)[0] + '.tif'
            output_asset = LocalAsset(
                region_id=asset.region_id,
                asset_id=asset.asset_id,
                path=destination,
                kind=AssetKind.RASTER,
                product=ProductType.IMAGERY,
                metadata={
                    **asset.metadata,
                    'crs': 'EPSG:3857',
                },
            )
            if options.skip_existing and os.path.isfile(destination):
                materialized.add_asset(output_asset, AssetStatus.SKIPPED)
                self._remove_download(asset.path)
                continue
            try:
                XYZGeoReferencer.convert(asset.path, destination)
                materialized.add_asset(output_asset, AssetStatus.SUCCESS)
                self._remove_download(asset.path)
            except Exception as exc:
                materialized.add_failure(Failure(asset.region_id, asset.asset_id, str(exc)))
        return materialized

    def reuse_existing(
        self,
        request: DownloadRequest,
        output_dir: str,
        existing_filenames: frozenset[str],
        options: AcquireOptions,
    ) -> AcquisitionReport | None:
        """Reuse a complete GeoTIFF result before downloading its source image."""
        if self.output_format != 'geotiff':
            return None
        raster_filename = os.path.splitext(request.filename)[0] + '.tif'
        required = {os.path.normcase(raster_filename)}
        if self.keep_download:
            required.add(os.path.normcase(request.filename))
        if not required.issubset(existing_filenames):
            return None
        report = AcquisitionReport.for_regions([request.region_id])
        report.add_asset(
            LocalAsset(
                region_id=request.region_id,
                asset_id=request.asset_id,
                path=os.path.join(output_dir, raster_filename),
                kind=AssetKind.RASTER,
                product=ProductType.IMAGERY,
                metadata={**request.metadata, 'crs': 'EPSG:3857'},
            ),
            AssetStatus.SKIPPED,
        )
        return report

    def _remove_download(self, path: str) -> None:
        if self.keep_download:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f'[cleanup] file={path} warning={exc}')
