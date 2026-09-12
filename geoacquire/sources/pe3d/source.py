#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: source.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: PE3D Source adapter for authenticated 1 m MDT quadrangle downloads.

import os
from collections.abc import Iterable

from geoacquire.core.acquisition import AcquisitionProgress
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
    Region,
)
from geoacquire.core.source import HTTPSource

from .catalog import PE3DQuadrangle, PE3DQuadrangleCatalog
from .client import PE3DClient, PE3DDownload
from .planner import PE3DSheet, plan_region_sheets
from .products import get_product_spec


class PE3DSource(HTTPSource):
    """Download PE3D 1:5,000 MDT rasters while preserving native tiles."""

    def __init__(
        self,
        username: str,
        password: str,
        product: str = 'dtm_raster',
        captcha_path: str = './cache/pe3d/captcha.png',
        catalog_cache_path: str = './cache/pe3d/quadriculas_pe.json',
        catalog_cache_days: float = 10.0,
        auth_attempts: int = 3,
        batch_size: int = 100,
        keep_archive: bool = False,
        verify_tls: bool = True,
        ca_bundle_path: str | None = None,
        quadrangle_mode: str = 'quad',
        min_intersection_fraction: float = 0.0,
    ):
        if not 0.0 <= min_intersection_fraction <= 1.0:
            raise ValueError('min_intersection_fraction must be between 0 and 1')
        self.product_spec = get_product_spec(product)
        self.keep_archive = keep_archive
        self.min_intersection_fraction = min_intersection_fraction
        # Construction stays network-free. Authentication starts only when an
        # uncached sheet needs remote discovery, so --check remains offline.
        self.client = PE3DClient(
            username=username,
            password=password,
            captcha_path=captcha_path,
            auth_attempts=auth_attempts,
            batch_size=batch_size,
            verify_tls=verify_tls,
            ca_bundle_path=ca_bundle_path,
            quadrangle_mode=quadrangle_mode,
        )
        self.catalog = PE3DQuadrangleCatalog(
            session=self.client.session,
            cache_path=catalog_cache_path,
            cache_days=catalog_cache_days,
            verify_tls=self.client.verify_tls,
        )

    @property
    def output_specs(self) -> frozenset[AssetSpec]:
        return frozenset({AssetSpec(self.product_spec.kind, self.product_spec.product)})

    def build_requests(self, regions: dict[str, Region]) -> Iterable[DownloadRequest]:
        raise RuntimeError('PE3DSource requires its authenticated acquire lifecycle')

    def acquire(
        self,
        regions: dict[str, Region],
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        output_root = context.resolve_acquire_output(options.output_dir, regions)
        os.makedirs(output_root, exist_ok=True)
        progress = AcquisitionProgress(
            regions,
            context.on_region_ready,
            quiet=context.reporting is not None,
        )
        existing_filenames = self.client.scan_filenames(output_root)
        requests = self._request_stream(
            regions,
            progress,
            output_root,
            existing_filenames,
            options,
        )
        return context.http.download(
            requests=requests,
            region_ids=regions.keys(),
            output_dir=output_root,
            skip_existing=options.skip_existing,
            max_retries=options.max_retries,
            max_workers=options.max_workers,
            chunk_size=options.chunk_size,
            retry_delay=options.retry_delay,
            materialize=lambda report: self.materialize(report, context, options),
            reuse_existing=lambda request, directory, filenames: self.reuse_existing(
                request,
                directory,
                filenames,
                options,
            ),
            progress=progress,
            reporting=context.reporting,
            pipeline_name=context.pipeline_name,
        )

    def materialize(
        self,
        report: AcquisitionReport,
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        output = AcquisitionReport.for_regions(report.regions)
        output.copy_failures_from(report)
        for _, archive in report.iter_assets():
            sheet_code = str(archive.metadata['sheet_code'])
            destination = self.client.extract_archive(
                archive.path,
                sheet_code,
                os.path.dirname(archive.path),
                self.product_spec,
            )
            if not self.keep_archive:
                os.remove(archive.path)
            output.add_asset(
                LocalAsset(
                    region_id=archive.region_id,
                    asset_id=archive.asset_id,
                    path=destination,
                    kind=self.product_spec.kind,
                    product=self.product_spec.product,
                    metadata={**archive.metadata},
                ),
                AssetStatus.SUCCESS,
            )
        return output

    def reuse_existing(
        self,
        request: DownloadRequest,
        output_dir: str,
        existing_filenames: frozenset[str],
        options: AcquireOptions,
    ) -> AcquisitionReport | None:
        sheet_code = str(request.metadata['sheet_code'])
        asset_name = self._asset_filename(sheet_code)
        if os.path.normcase(asset_name) not in existing_filenames:
            return None
        return self._local_report(
            request.region_id,
            sheet_code,
            os.path.join(output_dir, asset_name),
            AssetStatus.SKIPPED,
            request.metadata,
        )

    def _request_stream(
        self,
        regions: dict[str, Region],
        progress: AcquisitionProgress,
        output_root: str,
        existing_filenames: frozenset[str],
        options: AcquireOptions,
    ) -> Iterable[DownloadRequest]:
        product = self.product_spec
        unique_sheets: dict[str, PE3DSheet] = {}
        target_regions: dict[str, list[str]] = {}

        # Finish all geometry planning before authentication. This makes one
        # login serve the whole vector dataset and lets link discovery batch
        # unique quadrangles across feature boundaries.
        for region_id, region in regions.items():
            sheets = plan_region_sheets(region, self.min_intersection_fraction)
            for sheet in sheets:
                unique_sheets.setdefault(sheet.code, sheet)
                targets = target_regions.setdefault(sheet.code, [])
                if region_id not in targets:
                    targets.append(region_id)
                progress.register(self._file_key(sheet.code), [region_id])
        for region_id in regions:
            progress.close_region(region_id)

        remote_sheets: list[PE3DSheet] = []
        for sheet in unique_sheets.values():
            asset_name = self._asset_filename(sheet.code)
            if options.skip_existing and os.path.normcase(asset_name) in existing_filenames:
                owner = target_regions[sheet.code][0]
                progress.complete(
                    self._file_key(sheet.code),
                    self._local_report(
                        owner,
                        sheet.code,
                        os.path.join(output_root, asset_name),
                        AssetStatus.SKIPPED,
                        self._sheet_metadata(sheet),
                    ),
                )
            else:
                remote_sheets.append(sheet)

        if not remote_sheets:
            return

        catalog_by_sheet = self.catalog.by_sheet()
        catalogued: list[tuple[PE3DSheet, PE3DQuadrangle]] = []
        for sheet in remote_sheets:
            quadrangle = catalog_by_sheet.get(sheet.code)
            if quadrangle is not None:
                catalogued.append((sheet, quadrangle))
                continue
            owner = target_regions[sheet.code][0]
            failure = AcquisitionReport.for_regions([owner])
            failure.add_failure(Failure(
                owner,
                f'pe3d:{product.key}:{sheet.code}',
                'PE3D current quadrangle catalogue has no available entry for this sheet',
                self.catalog.URL,
            ))
            progress.complete(self._file_key(sheet.code), failure)

        resolved: dict[str, PE3DDownload] = {}
        if catalogued:
            self.client.authenticate()
        for start in range(0, len(catalogued), self.client.batch_size):
            batch = catalogued[start:start + self.client.batch_size]
            downloads = self.client.list_downloads(
                (quadrangle for _, quadrangle in batch),
                product,
            )
            found = {download.sheet_code: download for download in downloads}
            for sheet, _ in batch:
                download = found.get(sheet.code)
                if download is not None:
                    resolved[sheet.code] = download
                    continue
                owner = target_regions[sheet.code][0]
                failure = AcquisitionReport.for_regions([owner])
                failure.add_failure(Failure(
                    owner,
                    f'pe3d:{product.key}:{sheet.code}',
                    'PE3D returned no verified 1:5,000 MDT archive link for this sheet',
                    self.client.DOWNLOAD_LIST_URL,
                ))
                progress.complete(self._file_key(sheet.code), failure)

        headers = self.client.download_headers() if resolved else {}
        for sheet in unique_sheets.values():
            download = resolved.get(sheet.code)
            if download is None:
                continue
            targets = tuple(target_regions[sheet.code])
            yield DownloadRequest(
                region_id=targets[0],
                asset_id=f'pe3d:{product.key}:{sheet.code}',
                url=download.url,
                filename=download.filename,
                kind=AssetKind.FILE,
                product=product.product,
                headers=headers,
                metadata=self._sheet_metadata(sheet),
                target_region_ids=targets,
                verify_tls=self.client.verify_tls,
            )

    def _local_report(
        self,
        region_id: str,
        sheet_code: str,
        path: str,
        status: AssetStatus,
        metadata: dict[str, object],
    ) -> AcquisitionReport:
        report = AcquisitionReport.for_regions([region_id])
        report.add_asset(
            LocalAsset(
                region_id=region_id,
                asset_id=f'pe3d:{self.product_spec.key}:{sheet_code}',
                path=path,
                kind=self.product_spec.kind,
                product=self.product_spec.product,
                metadata=metadata,
            ),
            status,
        )
        return report

    def _sheet_metadata(self, sheet: PE3DSheet) -> dict[str, object]:
        zone = int(sheet.code.split('-')[1])
        return {
            'sheet_code': sheet.code,
            'bounds_wgs84': sheet.bounds_wgs84,
            'portal_product': self.product_spec.key,
            'portal_product_code': self.product_spec.site_code,
            'scale': '1:5000',
            'resolution': self.product_spec.resolution,
            'source_crs': f'EPSG:{31960 + zone}',
            'vertical_crs': None,
            'vertical_datum': 'unknown',
        }

    def _asset_filename(self, sheet_code: str) -> str:
        product = self.product_spec
        assert product.archive_prefix is not None
        assert product.primary_extension is not None
        return f'{product.archive_prefix}{sheet_code}{product.primary_extension}'

    def _file_key(self, sheet_code: str) -> str:
        prefix = self.product_spec.archive_prefix
        assert prefix is not None
        return os.path.normcase(f'{prefix}{sheet_code}.zip')
