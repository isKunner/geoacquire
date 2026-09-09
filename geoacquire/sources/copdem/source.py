#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: source.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Complete CopDEM Source adapter that owns the stateful CDSE acquisition lifecycle.

import time

from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.context import RuntimeContext
from geoacquire.core.models import (
    AcquisitionReport,
    AcquireOptions,
    AssetKind,
    AssetSpec,
    Failure,
    LocalAsset,
    ProductType,
    Region,
)
from geoacquire.core.source import BaseSource

from .client import CopDEMClient
from .planner import plan_region_tiles


class CopDEMSource(BaseSource):
    """Plan, authenticate, search, download, and extract Copernicus DEM tiles."""

    OUTPUT_SPECS = frozenset({AssetSpec(AssetKind.RASTER, ProductType.DEM)})

    def __init__(
        self,
        username: str,
        password: str,
        resolution: str = '30',
        dem_format: str = 'DGED',
        keep_archive: bool = False,
        filename_suffix: str = 'copdem',
    ):
        self.keep_archive = keep_archive
        # Construction validates options but stays network-free, so --check
        # never authenticates. The same stateful client is reused by acquire().
        self.client = CopDEMClient(
            username,
            password,
            resolution,
            dem_format,
            filename_suffix=filename_suffix,
        )

    def acquire(
        self,
        regions: dict[str, Region],
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        progress = AcquisitionProgress(regions, context.on_region_ready)
        acquired: set[str] = set()
        client = self.client
        # download_tile authenticates lazily when remote search is necessary.
        # Empty inputs and already extracted local DEMs need no login.
        output_root = context.resolve_acquire_output(options.output_dir, regions)
        existing_filenames = client.scan_filenames(output_root)

        for region_id, region in regions.items():
            tiles = plan_region_tiles(region, client.resolution)
            for tile_name in tiles:
                progress.register(tile_name, [region_id])
            progress.close_region(region_id)
            for index, tile_name in enumerate(tiles, 1):
                if tile_name in acquired:
                    print(f'[copdem] region={region_id} tile={tile_name} action=reuse')
                    continue
                acquired.add(tile_name)
                report = AcquisitionReport.for_regions([region_id])
                print(f'[copdem] region={region_id} tile={index}/{len(tiles)} name={tile_name}')
                last_error = ''
                for attempt in range(options.max_retries + 1):
                    try:
                        status, path, product_id = client.download_tile(
                            tile_name=tile_name,
                            output_dir=output_root,
                            skip_existing=options.skip_existing,
                            keep_archive=self.keep_archive,
                            chunk_size=options.chunk_size,
                            existing_filenames=existing_filenames,
                        )
                        asset = LocalAsset(
                            region_id=region_id,
                            asset_id=tile_name,
                            path=path,
                            kind=AssetKind.RASTER,
                            product=ProductType.DEM,
                            metadata={
                                'product_id': product_id,
                                'resolution': client.resolution,
                                'dem_format': client.dem_format,
                            },
                        )
                        report.add_asset(asset, status)
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        if attempt < options.max_retries and options.retry_delay > 0:
                            time.sleep(options.retry_delay * (2 ** attempt))
                else:
                    report.add_failure(Failure(region_id, tile_name, last_error or 'Unknown error'))
                # download_tile includes ZIP extraction. Notify only after that
                # usable DEM is closed, then continue acquiring the next tile.
                progress.complete(tile_name, report)
        return progress.report
