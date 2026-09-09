#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: models.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Stable data contracts shared by regions, sources, services, and post-processors.

import math
import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable


class AssetKind(StrEnum):
    """Closed storage/processing forms understood by GeoAcquire core."""

    FILE = 'file'
    IMAGE = 'image'
    RASTER = 'raster'
    POINT_CLOUD = 'point_cloud'


class ProductType(StrEnum):
    """Canonical built-in product types; custom Sources may use other strings."""

    GENERIC = 'generic'
    IMAGERY = 'imagery'
    DEM = 'dem'
    LAZ = 'laz'
    DSM = 'dsm'
    DTM = 'dtm'


class AssetStatus(StrEnum):
    """Stable acquisition outcomes shared by Sources, services, and reports."""

    SUCCESS = 'success'
    SKIPPED = 'skipped'
    REUSED = 'reused'
    FAILED = 'failed'


class RegionMetadataKey(StrEnum):
    """Cross-layer keys for the optional target-raster contract on Region."""

    TARGET_PATH = 'target_path'
    SHAPE = 'shape'
    TRANSFORM = 'transform'


def validate_product_type(value: str, label: str = 'product') -> str:
    """Validate one open-ended product-type label."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a non-empty string')
    if value != value.strip():
        raise ValueError(f'{label} must not contain leading or trailing whitespace')
    return value


@dataclass(frozen=True)
class AssetSpec:
    """One kind/product pair a Source or Postprocessor promises it can produce."""

    kind: AssetKind
    product: str

    def __post_init__(self) -> None:
        object.__setattr__(self, 'kind', AssetKind(self.kind))
        validate_product_type(self.product, 'asset product')


def validate_path_component(value: str, label: str) -> None:
    """Validate a value that will become one component of an output path.

    Region IDs and downloaded filenames are joined below the configured output
    directory. Rejecting absolute paths, parent-directory markers, and nested
    paths prevents one Source from accidentally writing outside that directory.
    """
    if not value or value in ('.', '..') or os.path.basename(value) != value or os.path.isabs(value):
        raise ValueError(f'{label} must be a non-empty path component, got: {value!r}')


# Input boundary: RegionProvider creates Regions, then every Source and
# Postprocessor consumes the same normalized geographic representation.
@dataclass(frozen=True)
class Region:
    """One normalized geographic area to acquire.

    Created by:
        BaseRegionProvider implementations.
    Consumed by:
        Sources for coverage planning and Postprocessors for target alignment.

    Attributes:
        region_id: Stable name for the area, also used in final output filenames.
            It does not create a download subdirectory.
        bounds: Extent in (minx, miny, maxx, maxy) order, expressed in `crs`.
        crs: Coordinate reference system describing `bounds`.
        metadata: Optional extra input information. A target-raster Region stores
            target_path, shape, and transform here.
    """

    region_id: str
    bounds: tuple[float, float, float, float]
    crs: str = 'EPSG:4326'
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_path_component(self.region_id, 'region_id')
        if len(self.bounds) != 4:
            raise ValueError(f'bounds must contain four values, got {len(self.bounds)}')
        if not all(math.isfinite(value) for value in self.bounds):
            raise ValueError(f'bounds must contain only finite values, got {self.bounds}')
        minx, miny, maxx, maxy = self.bounds
        if minx >= maxx or miny >= maxy:
            raise ValueError(f'bounds must satisfy min < max, got {self.bounds}')
        if not self.crs:
            raise ValueError('crs must not be empty')

    @property
    def target_path(self) -> str | None:
        """Target raster path, or None for a pure bounds Region."""
        value = self.metadata.get(RegionMetadataKey.TARGET_PATH)
        return str(value) if value else None

    @property
    def target_shape(self) -> tuple[int, int] | None:
        """Target (rows, columns), when this Region came from a raster."""
        value = self.metadata.get(RegionMetadataKey.SHAPE)
        return tuple(value) if value is not None else None

    @property
    def target_transform(self) -> tuple[float, ...] | None:
        """Target affine transform coefficients, when available."""
        value = self.metadata.get(RegionMetadataKey.TRANSFORM)
        return tuple(value) if value is not None else None


# Runtime configuration boundary: one instance is created from each pipeline's
# `acquire` YAML section and passed to the selected Source.
@dataclass(frozen=True)
class AcquireOptions:
    """Common execution options for one Source acquisition.

    Attributes:
        output_dir: Download root. null uses the common target-TIF directory,
            or workspace/output when Regions have no target file. Relative
            explicit paths are resolved from the workspace directory.
        skip_existing: Treat an existing destination as a usable skipped asset
            instead of downloading or materializing it again.
        max_retries: Additional attempts after the first failed attempt.
        max_workers: Maximum concurrent download-plus-conversion file tasks.
            A custom BaseSource such as CopDEM may deliberately remain serial.
        chunk_size: Number of bytes read and written per streaming download chunk.
        retry_delay: Base delay in seconds for exponential retry backoff.
    """

    output_dir: str | None = None
    skip_existing: bool = True
    max_retries: int = 3
    max_workers: int = 4
    chunk_size: int = 1024 * 1024
    retry_delay: float = 2.0

    def __post_init__(self) -> None:
        if self.output_dir is not None and not self.output_dir:
            raise ValueError('output_dir must be null or a non-empty path')
        if self.max_retries < 0:
            raise ValueError('max_retries must be >= 0')
        if self.max_workers < 1:
            raise ValueError('max_workers must be >= 1')
        if self.chunk_size < 1:
            raise ValueError('chunk_size must be >= 1')
        if self.retry_delay < 0:
            raise ValueError('retry_delay must be >= 0')


# HTTP planning boundary: HTTPSource creates DownloadRequests and hands them to
# HTTPDownloadService. These objects describe remote transfers, not final output.
@dataclass(frozen=True)
class DownloadRequest:
    """One remote file transfer prepared by an HTTPSource.

    Created by:
        HTTPSource.build_requests(), lazily as the downloader requests more work.
    Consumed by:
        HTTPDownloadService.download().

    Attributes:
        region_id: First Region requesting this shared physical file.
        asset_id: Source-defined logical identity used in reports and failures.
            It does not have to equal the local filename.
        url: Complete remote URL requested by the shared HTTP service.
        filename: Safe local filename directly below output_dir.
        kind: Closed storage/processing form from AssetKind.
        product: Case-sensitive description of what the requested result is.
            Built-in Sources use ProductType; custom Sources may use other
            non-empty strings. Product is not inferred from filename or kind.
        headers: Optional request-specific HTTP headers.
        metadata: Source information that must survive into the LocalAsset, such
            as XYZ coordinates or source tile keys.
        target_region_ids: Every Region that consumes this physical destination.
            Empty input is normalized to ``(region_id,)``. The HTTP service owns
            dependency registration and destination deduplication.
    """

    region_id: str
    asset_id: str
    url: str
    filename: str
    kind: AssetKind = AssetKind.FILE
    product: str = ProductType.GENERIC
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    target_region_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_path_component(self.region_id, 'region_id')
        validate_path_component(self.filename, 'filename')
        if not self.asset_id:
            raise ValueError('asset_id must not be empty')
        if not self.url:
            raise ValueError('url must not be empty')
        object.__setattr__(self, 'kind', AssetKind(self.kind))
        validate_product_type(self.product)
        targets = tuple(dict.fromkeys(self.target_region_ids or (self.region_id,)))
        for target in targets:
            validate_path_component(target, 'target_region_id')
        object.__setattr__(self, 'target_region_ids', targets)


# Local asset boundary: every Source returns LocalAssets, regardless of whether
# it used the shared HTTP service, a stateful client, or local materialization.
@dataclass(frozen=True)
class LocalAsset:
    """One usable local source product returned by acquisition.

    Unlike DownloadRequest, a LocalAsset always points to a local file that a
    Postprocessor can consume. A downloaded JPG may become a GeoTIFF LocalAsset;
    a CopDEM ZIP is not returned, but its extracted DEM is.

    Attributes:
        region_id: Region that owns this local product.
        asset_id: Stable source-defined identity carried from planning.
        path: Local file path to the usable product.
        kind: Closed storage/processing form from AssetKind.
        product: Case-sensitive description of what the file contains. Built-in
            Sources use ProductType; custom Sources may define other non-empty
            strings. `kind` determines which processors can open the file;
            `product` lets a processor distinguish DSM, DTM, imagery, and other
            rasters that share the same technical form.
        metadata: Source-specific details needed by later materialization or
            target-oriented post-processing. Treat shared asset metadata as
            read-only; frozen dataclasses do not freeze nested dictionaries.
    """

    region_id: str
    asset_id: str
    path: str
    kind: AssetKind
    product: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'kind', AssetKind(self.kind))
        validate_product_type(self.product)


@dataclass(frozen=True)
class Failure:
    """One item that could not become a usable LocalAsset.

    Attributes:
        region_id: Region affected by the failure.
        asset_id: Logical item that failed.
        error: Human-readable final error after retries.
        url: Remote URL when the failure came from an HTTP transfer; custom
            client Sources may leave it as null.
    """

    region_id: str
    asset_id: str
    error: str
    url: str | None = None


# Per-region result: success, skipped, and reused all contain usable LocalAssets.
# They distinguish newly produced, pre-existing, and current-run shared files.
@dataclass
class RegionResult:
    """Acquisition outcome for one Region.

    Attributes:
        success: LocalAssets produced during the current run.
        skipped: Existing LocalAssets reused because skip_existing was enabled.
        failed: Items that remained unavailable after all attempts.
        reused: LocalAssets shared from another Region in the current run.
    """

    success: list[LocalAsset] = field(default_factory=list)
    skipped: list[LocalAsset] = field(default_factory=list)
    failed: list[Failure] = field(default_factory=list)
    reused: list[LocalAsset] = field(default_factory=list)

    @property
    def asset_count(self) -> int:
        """Count usable files without copying their lists for readiness/logging."""
        return len(self.success) + len(self.skipped) + len(self.reused)

    def usable(self) -> list[LocalAsset]:
        """Return all usable products in new/disk-reused/shared order."""
        return self.success + self.skipped + self.reused


# Source output boundary: Pipeline and Postprocessors only depend on this report,
# never on a Source-specific URL list, token, archive, or client response.
@dataclass
class AcquisitionReport:
    """Complete Source result grouped by Region ID.

    Created by:
        Every BaseSource.acquire() implementation.
    Consumed by:
        PipelineRunner and configured BasePostprocessor implementations.

    `regions` always maps a Region ID to its RegionResult. This common envelope
    lets HTTPSource, CopDEMSource, and future client-backed Sources participate in
    the same Pipeline without Source-specific branches. The report deliberately
    retains one LocalAsset or Failure per planned output because later
    Postprocessors need the complete collection of local paths.
    """

    regions: dict[str, RegionResult] = field(default_factory=dict)

    @classmethod
    def for_regions(cls, region_ids: Iterable[str]) -> 'AcquisitionReport':
        """Pre-create empty ledgers for all expected region IDs."""
        return cls({region_id: RegionResult() for region_id in region_ids})

    def ensure_region(self, region_id: str) -> RegionResult:
        """Lazy-init: create an empty RegionResult if this region_id is not yet tracked."""
        if region_id not in self.regions:
            self.regions[region_id] = RegionResult()
        return self.regions[region_id]

    def add_asset(self, asset: LocalAsset, status: AssetStatus | str) -> None:
        """Record a usable newly produced, disk-reused, or shared asset."""
        normalized = AssetStatus(status)
        if normalized is AssetStatus.FAILED:
            raise ValueError('failed items must be recorded with add_failure')
        result = self.ensure_region(asset.region_id)
        getattr(result, normalized.value).append(asset)

    def add_failure(self, failure: Failure) -> None:
        self.ensure_region(failure.region_id).failed.append(failure)

    def copy_failures_from(self, other: 'AcquisitionReport') -> None:
        """Copy failure entries without sharing mutable per-region lists."""
        for region_id, result in other.regions.items():
            self.ensure_region(region_id).failed.extend(result.failed)

    def iter_assets(self) -> Iterable[tuple[AssetStatus, LocalAsset]]:
        """Yield (status, asset) tuples across all regions."""
        for result in self.regions.values():
            for asset in result.success:
                yield AssetStatus.SUCCESS, asset
            for asset in result.skipped:
                yield AssetStatus.SKIPPED, asset
            for asset in result.reused:
                yield AssetStatus.REUSED, asset

    def summary(self) -> dict[str, int]:
        """Return total counts for every stable acquisition outcome."""
        return {
            'success': sum(len(result.success) for result in self.regions.values()),
            'skipped': sum(len(result.skipped) for result in self.regions.values()),
            'reused': sum(len(result.reused) for result in self.regions.values()),
            'failed': sum(len(result.failed) for result in self.regions.values()),
        }
