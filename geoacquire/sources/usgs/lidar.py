#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: lidar.py
# @Time    : 2026/8/30
# @Author  : Kevin
# @Describe: Catalogue-driven USGS LiDAR planning, downloading, and optional rasterization.

import json
import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import GeometryCollection, box
from shapely.ops import unary_union

from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.context import RuntimeContext
from geoacquire.core.geo import horizontal_crs, transform_bounds
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

from .base import USGSHTTPSource
from .lidar_catalog import LidarProjectCatalog, LidarProjectRule, LidarTileScheme
from .lidar_rasterizer import LidarRasterizer
from .lidar_tiles import parse_catalog_tile


@dataclass(frozen=True)
class _LidarTile:
    """One parsed LAZ filename with a deterministic source-CRS footprint."""

    url: str
    filename: str
    bounds: tuple[float, float, float, float]
    source_crs: str
    naming: str
    rule_name: str


@dataclass(frozen=True)
class _WorkunitPlan:
    """Cached result of parsing one USGS link list with one reviewed rule."""

    workunit: dict
    rule_name: str | None
    filenames: int
    tiles: tuple[_LidarTile, ...]
    unmatched: tuple[str, ...]
    duplicate_filenames: int = 0
    ignored_placeholders: int = 0
    error: str | None = None


class USGSLidarSource(USGSHTTPSource):
    """Acquire LAZ files using reviewed project rules, never remote-header guessing."""

    PRODUCT_SPECS = {
        ProductType.LAZ.value: AssetSpec(AssetKind.POINT_CLOUD, ProductType.LAZ),
        ProductType.DSM.value: AssetSpec(AssetKind.RASTER, ProductType.DSM),
        ProductType.DTM.value: AssetSpec(AssetKind.RASTER, ProductType.DTM),
    }

    def __init__(
        self,
        output_products: list[str] | None = None,
        raster_resolution: float = 1.0,
        exclude_classes: list[int] | None = None,
        point_chunk_size: int = 2_000_000,
        keep_download: bool = True,
        project_catalog_path: str = './configs/usgs_lidar_projects.yaml',
        mode: str = 'download',
        audit_report_path: str = './logs/usgs_lidar_audit.json',
        wesm_cache_dir: str = './cache/wesm',
        link_cache_dir: str = './cache/link_lists',
        link_cache_days: float = 10.0,
    ):
        super().__init__(wesm_cache_dir, link_cache_dir, link_cache_days)
        selected_products = [ProductType.LAZ.value] if output_products is None else output_products
        if not selected_products:
            raise ValueError('output_products must be a non-empty list')
        self.output_products = list(dict.fromkeys(selected_products))
        invalid = sorted(set(self.output_products) - self.PRODUCT_SPECS.keys())
        if invalid:
            raise ValueError(f'Unsupported LiDAR output products: {invalid}')
        if mode not in ('download', 'audit'):
            raise ValueError("mode must be 'download' or 'audit'")
        self.keep_download = keep_download
        self.mode = mode
        self.audit_report_path = audit_report_path
        self.catalog = LidarProjectCatalog(project_catalog_path)
        self.rasterizer = LidarRasterizer(raster_resolution, exclude_classes, point_chunk_size)
        self._workunit_cache: dict[tuple[str, str, str, str], _WorkunitPlan] = {}
        self._scheme_crs_cache: dict[tuple[str, str, int | None], str | None] = {}
        self._audit: list[dict] = []
        self._runtime_reporting = None
        self._runtime_pipeline_name: str | None = None

    @property
    def output_specs(self) -> frozenset[AssetSpec]:
        if self.mode == 'audit':
            return frozenset()
        return frozenset(self.PRODUCT_SPECS[product] for product in self.output_products)

    def acquire(
        self,
        regions: dict[str, Region],
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        """Run an audit-only preflight or stream planned requests to HTTP workers."""
        self._audit = []
        self._runtime_reporting = context.reporting
        self._runtime_pipeline_name = context.pipeline_name

        if self.mode == 'audit':
            # Audit stops after rule parsing. It does not repeat tile selection,
            # coverage unions or download planning.
            self._audit_regions(regions)
            self._write_audit(context)
            print('[usgs/audit] mode=audit; no LAZ files were downloaded')
            self._runtime_reporting = None
            self._runtime_pipeline_name = None
            return AcquisitionReport.for_regions(regions)

        try:
            return super().acquire(regions, context, options)
        finally:
            self._runtime_reporting = None
            self._runtime_pipeline_name = None

    def build_requests(self, regions: dict[str, Region]):
        """Plan one shared destination pool for every target Region."""
        return self._build_requests(regions)

    def plan_requests(self, regions: dict[str, Region], progress: AcquisitionProgress):
        return self._build_requests(regions, progress)

    def _build_requests(self, regions: dict[str, Region], progress: AcquisitionProgress | None = None):
        batches = self._region_batches(regions)
        for label, batch in batches:
            self._detail('plan/batch', f'batch={label} target_tifs={len(batch)}')
            workunits_by_region = self.wesm.query_many(batch)
            yield from self._build_batch_requests(
                batch,
                workunits_by_region,
                progress,
            )

    def _build_batch_requests(
        self,
        regions: dict[str, Region],
        workunits_by_region: dict[str, list[dict]],
        progress: AcquisitionProgress | None = None,
    ):
        ordered = self._ordered_workunits(workunits_by_region)
        # A Region's list is final after its last candidate workunit, or earlier
        # when selected newer tiles cover it completely. Register ALL dependencies
        # from the current workunit before closing any Region.
        last_workunit = {
            region_id: index
            for index, (_, region_ids) in enumerate(ordered)
            for region_id in region_ids
        }
        closed: set[str] = set()
        if progress is not None:
            for region_id in regions.keys() - last_workunit.keys():
                progress.close_region(region_id)
        if not ordered:
            issue = {
                'status': 'no_workunits',
                'workunit': None,
                'error': 'No WESM LiDAR workunits intersect this batch',
                'targets': [
                    {
                        'region_id': region_id,
                        'target_path': regions[region_id].target_path,
                    }
                    for region_id in sorted(regions)
                ],
            }
            if self._runtime_reporting is not None and self._runtime_pipeline_name is not None:
                self._runtime_reporting.planning_issue(self._runtime_pipeline_name, issue)
            else:
                self._detail('plan/issue', issue['error'])
            return

        covered = {region_id: GeometryCollection() for region_id in regions}
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='usgs-plan') as planner:
            future = planner.submit(self._load_workunit_plan, ordered[0][0])
            for index, (workunit, region_ids) in enumerate(ordered):
                plan = future.result()
                if index + 1 < len(ordered):
                    future = planner.submit(self._load_workunit_plan, ordered[index + 1][0])

                self._record_audit(plan, region_ids, regions)
                self._detail(
                    'plan/workunit',
                    f'workunit={workunit.get("workunit", "unknown")} '
                    f'rule={plan.rule_name or "unmatched"} files={plan.filenames} '
                    f'parsed={len(plan.tiles)} unmatched={len(plan.unmatched)} '
                    f'duplicate_names={plan.duplicate_filenames} '
                    f'ignored_placeholders={plan.ignored_placeholders} '
                    f'target_tifs={len(region_ids)}'
                )
                if plan.error and self._runtime_reporting is None:
                    self._detail(
                        'plan/issue',
                        f'workunit={workunit.get("workunit", "unknown")} '
                        f'skipped_error={plan.error}'
                    )
                selected = self._select_tiles(
                    plan.tiles,
                    region_ids,
                    regions,
                    covered,
                ) if plan.tiles else OrderedDict()
                requests = self._requests_for_selected(selected, workunit)
                self._detail(
                    'plan/workunit',
                    f'workunit={workunit.get("workunit", "unknown")} '
                    f'selected={len(requests)}'
                )
                # HTTP registers every yielded request before asking this generator
                # for the next one. Yield all dependencies before closing Regions.
                for request in requests:
                    yield request
                if progress is not None:
                    for region_id in region_ids - closed:
                        shape = box(*regions[region_id].bounds)
                        remaining = shape.difference(covered[region_id])
                        if last_workunit[region_id] == index or remaining.area <= max(shape.area, 1.0) * 1e-9:
                            progress.close_region(region_id)
                            closed.add(region_id)

    def _audit_regions(self, regions: dict[str, Region]) -> None:
        """Parse each intersecting workunit once, without constructing downloads."""
        for label, batch in self._region_batches(regions):
            print(f'[usgs/audit] querying batch={label} regions={len(batch)}')
            ordered = self._ordered_workunits(self.wesm.query_many(batch))
            if not ordered:
                print('[usgs/audit] no WESM LiDAR workunits intersect this batch')
                continue

            # Link lists are independent, small text files. A few planning
            # workers make a first-time audit fast without creating LAZ traffic.
            worker_count = min(4, len(ordered))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix='usgs-audit',
            ) as planner:
                plans = planner.map(
                    self._load_workunit_plan,
                    (workunit for workunit, _ in ordered),
                )
                for (workunit, region_ids), plan in zip(ordered, plans):
                    self._record_audit(plan, region_ids, batch)
                    print(
                        f'[usgs/audit] workunit={workunit.get("workunit", "unknown")} '
                        f'rule={plan.rule_name or "unmatched"} files={plan.filenames} '
                        f'parsed={len(plan.tiles)} unmatched={len(plan.unmatched)} '
                        f'duplicate_names={plan.duplicate_filenames} '
                        f'ignored_placeholders={plan.ignored_placeholders}'
                    )
                    if plan.error:
                        print(
                            f'[usgs/audit] workunit={workunit.get("workunit", "unknown")} '
                            f'error={plan.error}'
                        )

    @staticmethod
    def _ordered_workunits(
        workunits_by_region: dict[str, list[dict]],
    ) -> list[tuple[dict, set[str]]]:
        """Deduplicate shared WESM rows and retain every intersecting target ID."""
        workunits: OrderedDict[tuple[str, str], tuple[dict, set[str]]] = OrderedDict()
        for region_id, items in workunits_by_region.items():
            for workunit in items:
                key = (
                    str(workunit.get('lpc_link') or ''),
                    str(workunit.get('workunit') or ''),
                )
                if key not in workunits:
                    workunits[key] = (workunit, set())
                workunits[key][1].add(region_id)
        return sorted(
            workunits.values(),
            key=lambda item: str(item[0].get('collect_end') or ''),
            reverse=True,
        )

    def _load_workunit_plan(self, workunit: dict) -> _WorkunitPlan:
        lpc_link = workunit.get('lpc_link')
        if not isinstance(lpc_link, str) or not lpc_link:
            return _WorkunitPlan(
                workunit=workunit,
                rule_name=None,
                filenames=0,
                tiles=(),
                unmatched=(),
                error='WESM workunit has no lpc_link',
            )

        rule = self.catalog.find(workunit)
        # A URL can be reused by WESM rows with different identities/CRSs.
        # Reuse the link text, but not a footprint parsed in another CRS.
        cache_key = (
            lpc_link, str(workunit.get('workunit') or ''),
            str(workunit.get('horiz_crs') or ''), rule.name if rule else '<unmatched>',
        )
        if cache_key in self._workunit_cache:
            return self._workunit_cache[cache_key]

        link_list_url = f'{lpc_link}/0_file_download_links.txt'
        try:
            text = self._fetch_text(link_list_url)
        except Exception as exc:
            plan = _WorkunitPlan(
                workunit=workunit,
                rule_name=rule.name if rule else None,
                filenames=0,
                tiles=(),
                unmatched=(),
                error=f'Cannot read link list {link_list_url}: {exc}',
            )
            self._workunit_cache[cache_key] = plan
            return plan

        raw_filename_count = 0
        ignored_placeholders = 0
        unique_urls: OrderedDict[str, str] = OrderedDict()
        for line in text.splitlines():
            url = self.normalize_link(line)
            filename = os.path.basename(urlparse(url).path)
            if filename.lower().endswith(('.las', '.laz')):
                raw_filename_count += 1
                if self._is_project_placeholder(filename):
                    ignored_placeholders += 1
                    continue
                unique_urls.setdefault(filename, url)
        urls = list(unique_urls.items())
        usable_filename_count = raw_filename_count - ignored_placeholders
        duplicate_filenames = usable_filename_count - len(urls)
        filename_count = len(urls)

        if rule is None:
            plan = _WorkunitPlan(
                workunit=workunit,
                rule_name=None,
                filenames=filename_count,
                tiles=(),
                unmatched=tuple(filename for filename, _ in urls),
                duplicate_filenames=duplicate_filenames,
                ignored_placeholders=ignored_placeholders,
                error='No project rule matched this WESM workunit',
            )
            self._workunit_cache[cache_key] = plan
            return plan

        tiles = []
        unmatched = []
        errors = []
        for filename, url in urls:
            tile = self._parse_rule_tile(filename, url, workunit, rule, errors)
            if tile is None:
                unmatched.append(filename)
            else:
                tiles.append(tile)
        plan = _WorkunitPlan(
            workunit=workunit,
            rule_name=rule.name,
            filenames=filename_count,
            tiles=tuple(tiles),
            unmatched=tuple(unmatched),
            duplicate_filenames=duplicate_filenames,
            ignored_placeholders=ignored_placeholders,
            error='; '.join(sorted(set(errors))) or None,
        )
        self._workunit_cache[cache_key] = plan
        return plan

    @staticmethod
    def _is_project_placeholder(filename: str) -> bool:
        """Reject catalogue rows whose project prefix has no tile identifier."""
        stem, _ = os.path.splitext(filename)
        token = stem.rstrip('_').rsplit('_', 1)[-1]
        # NC publishes real coordinate-coded names such as LA_37_10380906_.
        # Padding underscores alone do not make a numeric tile a placeholder.
        return stem.endswith('_') and not (token.isdigit() and len(token) >= 6)

    def _parse_rule_tile(
        self,
        filename: str,
        url: str,
        workunit: dict,
        rule: LidarProjectRule,
        errors: list[str],
    ) -> _LidarTile | None:
        for scheme in rule.schemes:
            parsed = parse_catalog_tile(filename, url, scheme.parser)
            if parsed is None:
                continue
            projected, mgrs_epsg = parsed
            source_crs = self._scheme_crs(scheme, workunit, mgrs_epsg)
            if source_crs is None:
                errors.append(f'Cannot resolve CRS for parser={scheme.parser}')
                continue
            return _LidarTile(
                url=url,
                filename=filename,
                bounds=scheme.grid.bounds(projected),
                source_crs=source_crs,
                naming=scheme.naming or scheme.parser,
                rule_name=rule.name,
            )
        return None

    def _scheme_crs(
        self,
        scheme: LidarTileScheme,
        workunit: dict,
        mgrs_epsg: int | None,
    ) -> str | None:
        # A link list can contain tens of thousands of files, but its workunit
        # CRS is constant. Constructing the same pyproj.CRS once per filename is
        # pure overhead. Cache the final string too: CRS.to_string() can query
        # PROJ's authority database, which is expensive per tile.
        workunit_crs = str(workunit.get('horiz_crs') or '')
        key = (scheme.crs, workunit_crs, mgrs_epsg)
        if key in self._scheme_crs_cache:
            return self._scheme_crs_cache[key]
        if scheme.crs == 'mgrs':
            resolved = CRS.from_epsg(mgrs_epsg) if mgrs_epsg is not None else None
        elif scheme.crs == 'workunit':
            resolved = self._workunit_crs(workunit)
        else:
            try:
                resolved = horizontal_crs(CRS.from_user_input(scheme.crs))
            except (CRSError, ValueError, TypeError):
                resolved = None
        result = resolved.to_string() if resolved is not None else None
        self._scheme_crs_cache[key] = result
        return result

    def _select_tiles(
        self,
        tiles: tuple[_LidarTile, ...],
        region_ids: set[str],
        regions: dict[str, Region],
        covered: dict[str, object],
    ) -> OrderedDict[str, tuple[_LidarTile, set[str]]]:
        selected: OrderedDict[str, tuple[_LidarTile, set[str]]] = OrderedDict()
        fully_covered = 0
        for region_id in sorted(region_ids):
            region = regions[region_id]
            region_shape = box(*region.bounds)
            remaining = region_shape.difference(covered[region_id])
            tolerance = max(region_shape.area, 1.0) * 1e-9
            if remaining.area <= tolerance:
                fully_covered += 1
                continue

            target_bounds: dict[str, tuple[float, float, float, float]] = {}
            footprints = []
            for tile in tiles:
                if tile.source_crs not in target_bounds:
                    target_bounds[tile.source_crs] = transform_bounds(
                        region.bounds,
                        region.crs,
                        tile.source_crs,
                    )
                if not self._intersects(tile.bounds, target_bounds[tile.source_crs]):
                    continue
                bounds = transform_bounds(tile.bounds, tile.source_crs, region.crs)
                footprint = box(*bounds).intersection(remaining)
                if footprint.area <= tolerance:
                    continue
                footprints.append(footprint)
                if tile.url not in selected:
                    selected[tile.url] = (tile, set())
                selected[tile.url][1].add(region_id)

            if footprints:
                covered[region_id] = unary_union((covered[region_id], *footprints))

        if fully_covered:
            self._detail(
                'plan/coverage',
                f'skipped current older workunit for {fully_covered} covered target TIF(s)',
            )
        return selected

    def _requests_for_selected(
        self,
        selected: OrderedDict[str, tuple[_LidarTile, set[str]]],
        workunit: dict | None = None,
    ) -> list[DownloadRequest]:
        """Create requests; the HTTP service owns filename deduplication."""
        requests: list[DownloadRequest] = []
        for _, (tile, region_ids) in selected.items():
            targets = set(region_ids)
            request = self._request(sorted(targets)[0], tile, targets, workunit)
            requests.append(request)
        return requests

    @staticmethod
    def _request(
        region_id: str,
        tile: _LidarTile,
        target_region_ids: set[str],
        workunit: dict | None = None,
    ) -> DownloadRequest:
        return DownloadRequest(
            region_id=region_id,
            asset_id=os.path.splitext(tile.filename)[0],
            url=tile.url,
            filename=tile.filename,
            kind=AssetKind.POINT_CLOUD,
            product=ProductType.LAZ,
            target_region_ids=tuple(sorted(target_region_ids)),
            metadata={
                'source_crs': tile.source_crs,
                'tile_bounds': tile.bounds,
                'tile_naming': tile.naming,
                'project_rule': tile.rule_name,
                'project': (workunit or {}).get('project'),
                'workunit': (workunit or {}).get('workunit'),
                'collect_end': str((workunit or {}).get('collect_end') or ''),
            },
        )

    @staticmethod
    def _intersects(
        left: tuple[float, float, float, float],
        right: tuple[float, float, float, float],
    ) -> bool:
        return left[0] < right[2] and left[2] > right[0] and left[1] < right[3] and left[3] > right[1]

    @staticmethod
    def _workunit_crs(workunit: dict) -> CRS | None:
        value = workunit.get('horiz_crs')
        if value is None or str(value).strip() == '':
            return None
        text = str(value).strip()
        candidates = [f'EPSG:{text}', f'ESRI:{text}'] if text.isdigit() else [text]
        for candidate in candidates:
            try:
                return horizontal_crs(CRS.from_user_input(candidate))
            except (CRSError, ValueError, TypeError):
                continue
        return None

    def _record_audit(
        self,
        plan: _WorkunitPlan,
        region_ids: set[str],
        regions: dict[str, Region],
    ) -> None:
        status = 'matched'
        if plan.rule_name is None or plan.error:
            status = 'unmatched'
        elif plan.unmatched:
            status = 'partial'
        if self.mode != 'audit' and status == 'matched':
            return
        item = {
            'status': status,
            'rule': plan.rule_name,
            'project': plan.workunit.get('project'),
            'workunit': plan.workunit.get('workunit'),
            'collect_end': str(plan.workunit.get('collect_end') or ''),
            'horiz_crs': plan.workunit.get('horiz_crs'),
            'workunit_bounds': list(plan.workunit.get('_wesm_bounds') or []),
            'workunit_bounds_crs': plan.workunit.get('_wesm_crs'),
            'lpc_link': plan.workunit.get('lpc_link'),
            'filename_count': plan.filenames,
            'parsed_count': len(plan.tiles),
            'unmatched_count': len(plan.unmatched),
            'duplicate_filename_count': plan.duplicate_filenames,
            'ignored_placeholder_count': plan.ignored_placeholders,
            'filename_examples': list(plan.unmatched[:10]),
            'error': plan.error,
            'targets': [
                {
                    'region_id': region_id,
                    'target_path': regions[region_id].target_path,
                    'bounds': list(regions[region_id].bounds),
                    'crs': regions[region_id].crs,
                }
                for region_id in sorted(region_ids)
            ],
        }
        self._audit.append(item)
        if (
            status != 'matched'
            and self._runtime_reporting is not None
            and self._runtime_pipeline_name is not None
        ):
            self._runtime_reporting.planning_issue(self._runtime_pipeline_name, item)

    def _write_audit(self, context: RuntimeContext) -> None:
        path = context.resolve_path(self.audit_report_path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temporary = path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as file:
            json.dump(self._audit, file, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        issues = sum(item['status'] != 'matched' for item in self._audit)
        print(f'[usgs/audit] report={path} entries={len(self._audit)} issues={issues}')

    def materialize(
        self,
        report: AcquisitionReport,
        context: RuntimeContext,
        options: AcquireOptions,
    ) -> AcquisitionReport:
        """Convert the worker's physical file; the progress ledger owns Region fanout.

        Example: shared.laz -> shared_dsm.tif with product='dsm'. Return this
        product once under the input asset's Region, even when several Regions
        need it. AcquisitionProgress supplies their aliases after conversion.
        """
        raster_products = [
            product
            for product in self.output_products
            if self.PRODUCT_SPECS[product].kind is AssetKind.RASTER
        ]
        materialized = AcquisitionReport.for_regions(report.regions)

        materialized.copy_failures_from(report)

        for status, asset in report.iter_assets():
            if ProductType.LAZ.value in self.output_products:
                materialized.add_asset(asset, status)

            output_paths = {
                product: os.path.splitext(asset.path)[0] + f'_{product}.tif'
                for product in raster_products
            }
            known_filenames = (
                context.reporting.scan_filenames(os.path.dirname(os.path.abspath(asset.path)))
                if context.reporting is not None
                else None
            )
            missing = [
                product
                for product, path in output_paths.items()
                if not (
                    options.skip_existing
                    and (
                        os.path.normcase(os.path.basename(path)) in known_filenames
                        if known_filenames is not None
                        else os.path.isfile(path)
                    )
                )
            ]
            for product, path in output_paths.items():
                if product not in missing:
                    materialized.add_asset(
                        self._raster_asset(asset, product, path),
                        AssetStatus.SKIPPED,
                    )

            try:
                if missing and self._runtime_reporting is None:
                    print(f'[usgs/materialize] file={os.path.basename(asset.path)} start={missing}')
                products = self.rasterizer.rasterize(
                    asset.path,
                    missing,
                    fallback_crs=asset.metadata.get('source_crs'),
                ) if missing else {}
                for product in missing:
                    path = output_paths[product]
                    self.rasterizer.save(path, products[product])
                    materialized.add_asset(
                        self._raster_asset(asset, product, path),
                        AssetStatus.SUCCESS,
                    )
                if not self.keep_download and ProductType.LAZ.value not in self.output_products:
                    self._remove_download(asset.path)
                if missing and self._runtime_reporting is None:
                    print(f'[usgs/materialize] file={os.path.basename(asset.path)} ready={missing}')
            except Exception as exc:
                materialized.add_failure(Failure(asset.region_id, asset.asset_id, str(exc)))
        return materialized

    def _detail(self, event: str, message: str) -> None:
        """Keep workunit and per-LAZ detail out of the summary-only terminal."""
        if self._runtime_reporting is not None:
            self._runtime_reporting.detail(event, message)
        else:
            print(f'[usgs/{event}] {message}')

    def reuse_existing(
        self,
        request: DownloadRequest,
        output_dir: str,
        existing_filenames: frozenset[str],
        options: AcquireOptions,
    ) -> AcquisitionReport | None:
        """Reuse every requested LAZ/raster product before transferring the LAZ."""
        stem = os.path.splitext(request.filename)[0]
        product_paths = {
            product: (
                os.path.join(output_dir, request.filename)
                if product == ProductType.LAZ.value
                else os.path.join(output_dir, f'{stem}_{product}.tif')
            )
            for product in self.output_products
        }
        required = {
            os.path.normcase(os.path.basename(path))
            for path in product_paths.values()
        }
        if self.keep_download:
            required.add(os.path.normcase(request.filename))
        if not required.issubset(existing_filenames):
            return None

        report = AcquisitionReport.for_regions([request.region_id])
        source_asset = LocalAsset(
            region_id=request.region_id,
            asset_id=request.asset_id,
            path=os.path.join(output_dir, request.filename),
            kind=AssetKind.POINT_CLOUD,
            product=ProductType.LAZ,
            metadata=dict(request.metadata),
        )
        for product, path in product_paths.items():
            asset = source_asset if product == ProductType.LAZ.value else self._raster_asset(
                source_asset,
                product,
                path,
            )
            report.add_asset(asset, AssetStatus.SKIPPED)
        return report

    def _remove_download(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._detail('cleanup-warning', f'file={path} warning={exc}')

    @staticmethod
    def _raster_asset(
        source: LocalAsset,
        product: str,
        path: str,
    ) -> LocalAsset:
        return LocalAsset(
            region_id=source.region_id,
            asset_id=f'{source.asset_id}:{product}',
            path=path,
            kind=AssetKind.RASTER,
            product=ProductType(product),
            metadata=dict(source.metadata),
        )
