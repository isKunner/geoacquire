#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: http_download.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Source-neutral concurrent HTTP downloader with resume and retry support.

import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import BoundedSemaphore, Lock, local
from typing import TYPE_CHECKING

import requests

_REQUESTS_MODULE = requests
_DOWNLOAD_CONTEXT = local()

from geoacquire.core.acquisition import AcquisitionProgress
from geoacquire.core.models import (
    AcquisitionReport,
    AssetStatus,
    DownloadRequest,
    Failure,
    LocalAsset,
)

from .http_stream import is_complete_range, write_response_body

if TYPE_CHECKING:
    from .run_reporting import RunReportingService


@dataclass(frozen=True)
class _TransferResult:
    request: DownloadRequest
    status: AssetStatus
    path: str
    error: str | None = None
    attempts: int = 0
    elapsed_seconds: float = 0.0
    resumed_from_bytes: int = 0
    bytes_on_disk: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, 'status', AssetStatus(self.status))


class HTTPDownloadService:
    """Bounded file workers: download, convert, publish, then take the next file.

    The caller produces requests; each worker publishes its own completion.
    A slow next(request_iterator) therefore cannot hide completed files from
    Pipeline. No extra conversion pool and no whole-batch materialization pass.
    """

    PENDING_PER_WORKER = 4

    def download(
        self,
        requests: Iterable[DownloadRequest],
        region_ids: Iterable[str],
        output_dir: str,
        skip_existing: bool = True,
        max_retries: int = 3,
        max_workers: int = 4,
        chunk_size: int = 1024 * 1024,
        retry_delay: float = 2.0,
        materialize: Callable[[AcquisitionReport], AcquisitionReport] | None = None,
        reuse_existing: Callable[
            [DownloadRequest, str, frozenset[str]],
            AcquisitionReport | None,
        ] | None = None,
        progress: AcquisitionProgress | None = None,
        reporting: 'RunReportingService | None' = None,
        pipeline_name: str | None = None,
    ) -> AcquisitionReport:
        if max_workers < 1:
            raise ValueError('max_workers must be >= 1')
        os.makedirs(output_dir, exist_ok=True)
        existing_filenames = (
            reporting.scan_filenames(output_dir)
            if reporting is not None
            else self._scan_filenames(output_dir)
        )
        progress = progress or AcquisitionProgress(region_ids)
        request_iterator = iter(requests)
        destinations: dict[str, DownloadRequest] = {}
        max_pending = max_workers * self.PENDING_PER_WORKER
        slots = BoundedSemaphore(max_pending)
        stats_lock = Lock()
        submitted_count = 0
        completed_count = 0
        duplicate_count = 0
        worker_errors: list[Exception] = []
        worker_state = local()
        sessions: list[requests.Session] = []
        sessions_lock = Lock()
        observable = reporting is not None and pipeline_name is not None
        if not observable:
            print(f'[http] workers={max_workers} pending_limit={max_pending} task=download+materialize')

        def worker_session():
            session = getattr(worker_state, 'session', None)
            if session is None:
                session = _REQUESTS_MODULE.Session()
                worker_state.session = session
                with sessions_lock:
                    sessions.append(session)
            return session

        def acquire_file(request: DownloadRequest) -> None:
            nonlocal completed_count
            key = self._destination_key(request)
            try:
                _DOWNLOAD_CONTEXT.existing_filenames = existing_filenames
                _DOWNLOAD_CONTEXT.session = worker_session()
                if observable:
                    reporting.worker_started(pipeline_name, key)
                result = self._acquire_one(
                    request, output_dir, skip_existing, chunk_size,
                    max_retries, retry_delay, materialize, reuse_existing,
                    existing_filenames, reporting, pipeline_name, key,
                )
                if observable:
                    reporting.file_finished(
                        pipeline_name, key, request.filename,
                        request.target_region_ids, result,
                    )
                progress.complete(key, result)
                with stats_lock:
                    completed_count += 1
                    if not observable and (completed_count == 1 or completed_count % 50 == 0):
                        status = 'failed' if result.summary()['failed'] else 'success'
                        print(
                            f'[http] completed={completed_count} '
                            f'pending={submitted_count - completed_count} '
                            f'submitted={submitted_count} result={status} '
                            f'last={request.filename}'
                        )
            except Exception as exc:
                # Contract/coordinator errors must not disappear in an unobserved
                # Future. Ordinary download/conversion failures are report entries.
                with stats_lock:
                    worker_errors.append(exc)
            finally:
                slots.release()

        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='acquire') as executor:
                try:
                    while True:
                        # Acquire BEFORE next(): at most max_workers * 4 file jobs are
                        # produced/submitted, even when a generator is very fast.
                        slots.acquire()
                        try:
                            request = next(request_iterator)
                        except StopIteration:
                            slots.release()
                            progress.close_all()
                            if observable:
                                reporting.plan_finished(pipeline_name)
                            break
                        except BaseException:
                            slots.release()
                            raise
                        key = self._destination_key(request)
                        progress.register(key, request.target_region_ids)
                        kept = destinations.get(key)
                        if observable:
                            reporting.record_request(
                                pipeline_name,
                                key,
                                request,
                                duplicate=kept is not None,
                                partial_file_found=(
                                    os.path.normcase(request.filename + '.part') in existing_filenames
                                ),
                            )
                        if kept is not None:
                            duplicate_count += 1
                            if not observable:
                                print(
                                    f'[http] duplicate destination={key} action=reuse '
                                    f'kept_asset={kept.asset_id} duplicate_asset={request.asset_id} '
                                    f'kept_url={kept.url} duplicate_url={request.url}'
                                )
                            slots.release()
                            continue
                        destinations[key] = request
                        with stats_lock:
                            submitted_count += 1
                        executor.submit(acquire_file, request)
                except KeyboardInterrupt:
                    # Persist the current dependency graph before executor
                    # shutdown waits for in-flight network calls to unwind.
                    if observable:
                        reporting.fail_run('Interrupted by user', interrupted=True)
                    raise
        finally:
            for session in sessions:
                session.close()

        if worker_errors:
            raise RuntimeError('Acquisition completion failed') from worker_errors[0]

        if submitted_count and not observable:
            summary = progress.report.summary()
            print(
                f'[http] finished={completed_count} success={summary["success"]} '
                f'skipped={summary["skipped"]} reused={summary["reused"]} '
                f'failed={summary["failed"]} '
                f'reused_duplicates={duplicate_count}'
            )

        return progress.report

    @staticmethod
    def _destination_key(
        request: DownloadRequest,
    ) -> str:
        return os.path.normcase(request.filename)

    def _acquire_one(
        self,
        request: DownloadRequest,
        output_dir: str,
        skip_existing: bool,
        chunk_size: int,
        max_retries: int,
        retry_delay: float,
        materialize: Callable[[AcquisitionReport], AcquisitionReport] | None,
        reuse_existing: Callable[
            [DownloadRequest, str, frozenset[str]],
            AcquisitionReport | None,
        ] | None,
        existing_filenames: frozenset[str],
        reporting: 'RunReportingService | None' = None,
        pipeline_name: str | None = None,
        destination_key: str | None = None,
    ) -> AcquisitionReport:
        """Transfer and Source conversion stay in the SAME executor task/thread."""
        report = AcquisitionReport.for_regions([request.region_id])
        observable = reporting is not None and pipeline_name is not None and destination_key is not None
        try:
            if skip_existing and reuse_existing is not None:
                reuse_started = time.perf_counter()
                reused = reuse_existing(request, output_dir, existing_filenames)
                if reused is not None:
                    if observable:
                        assets = [asset for _, asset in reused.iter_assets()]
                        path = assets[0].path if assets else os.path.join(output_dir, request.filename)
                        reporting.transfer_finished(
                            pipeline_name, destination_key, 'existing', path, 0,
                            time.perf_counter() - reuse_started,
                        )
                    return reused
            transfer = self._download_with_retries(
                request, output_dir, skip_existing, chunk_size, max_retries, retry_delay,
            )
            if observable:
                reporting.transfer_finished(
                    pipeline_name,
                    destination_key,
                    transfer.status.value,
                    transfer.path,
                    transfer.attempts,
                    transfer.elapsed_seconds,
                    transfer.resumed_from_bytes,
                    transfer.bytes_on_disk,
                    transfer.error,
                )
            self._append_result(report, transfer)
            if transfer.status is not AssetStatus.FAILED and materialize is not None:
                materialize_started = time.perf_counter()
                report = materialize(report)
                if observable:
                    reporting.materialize_finished(
                        pipeline_name,
                        destination_key,
                        time.perf_counter() - materialize_started,
                    )
        except Exception as exc:
            # A raw transfer is not a usable converted product if conversion
            # raised. Do not count that intermediate file as a successful asset.
            report = AcquisitionReport.for_regions([request.region_id])
            report.add_failure(Failure(request.region_id, request.asset_id, str(exc), request.url))
        if not observable:
            for result in report.regions.values():
                for failure in result.failed:
                    print(f'[acquire/file] file={request.filename} failed: {failure.error}')
        return report

    def _download_with_retries(
        self,
        request: DownloadRequest,
        output_dir: str,
        skip_existing: bool,
        chunk_size: int,
        max_retries: int,
        retry_delay: float,
    ) -> _TransferResult:
        """Keep one request and all of its retry state inside a single worker."""
        started = time.perf_counter()
        result: _TransferResult | None = None
        for attempt in range(max_retries + 1):
            if attempt > 0 and retry_delay > 0:
                time.sleep(retry_delay * (2 ** (attempt - 1)))
            result = self._download_one(
                request,
                output_dir,
                skip_existing,
                chunk_size,
            )
            if result.status is not AssetStatus.FAILED:
                return replace(
                    result,
                    attempts=attempt + 1,
                    elapsed_seconds=time.perf_counter() - started,
                )
        assert result is not None
        return replace(
            result,
            attempts=max_retries + 1,
            elapsed_seconds=time.perf_counter() - started,
        )

    @staticmethod
    def _download_one(
        request: DownloadRequest,
        output_dir: str,
        skip_existing: bool,
        chunk_size: int,
    ) -> _TransferResult:
        destination = os.path.join(output_dir, request.filename)
        existing_filenames = getattr(_DOWNLOAD_CONTEXT, 'existing_filenames', None)
        if existing_filenames is None:
            existing_filenames = HTTPDownloadService._scan_filenames(output_dir)
        if skip_existing and os.path.normcase(request.filename) in existing_filenames:
            return _TransferResult(request, AssetStatus.SKIPPED, destination)

        part_path = destination + '.part'
        try:
            resume_byte = os.path.getsize(part_path)
        except FileNotFoundError:
            resume_byte = 0
        headers = {**request.headers, 'Accept-Encoding': 'identity'}
        if resume_byte > 0:
            headers['Range'] = f'bytes={resume_byte}-'

        try:
            session = getattr(_DOWNLOAD_CONTEXT, 'session', None)
            if request.method == 'POST':
                transfer = session.post if session is not None else requests.post
            else:
                transfer = session.get if session is not None else requests.get
            with transfer(
                request.url,
                headers=headers,
                data=request.data or None,
                stream=True,
                timeout=(30, 600),
                verify=request.verify_tls,
            ) as response:
                if is_complete_range(response, resume_byte):
                    os.replace(part_path, destination)
                    return _TransferResult(
                        request, AssetStatus.SUCCESS, destination,
                        resumed_from_bytes=resume_byte,
                        bytes_on_disk=resume_byte,
                    )
                response.raise_for_status()

                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type:
                    raise RuntimeError(f'Unexpected Content-Type: {content_type}')

                bytes_on_disk = write_response_body(
                    response, part_path, resume_byte, chunk_size,
                )

            os.replace(part_path, destination)
            return _TransferResult(
                request, AssetStatus.SUCCESS, destination,
                resumed_from_bytes=resume_byte,
                bytes_on_disk=bytes_on_disk,
            )
        except Exception as exc:
            return _TransferResult(request, AssetStatus.FAILED, destination, str(exc))

    @staticmethod
    def _append_result(report: AcquisitionReport, result: _TransferResult) -> None:
        request = result.request
        if result.status is AssetStatus.FAILED:
            report.add_failure(Failure(
                request.region_id,
                request.asset_id,
                result.error or 'Unknown error',
                request.url,
            ))
            return

        asset = LocalAsset(
            region_id=request.region_id,
            asset_id=request.asset_id,
            path=result.path,
            kind=request.kind,
            product=request.product,
            # Target membership belongs to the planning ledger. Do not hand a
            # mutable target set to the conversion worker: later workunits can
            # discover more aliases while this file is already being rasterized.
            metadata={
                **request.metadata,
            },
        )
        report.add_asset(asset, result.status)

    @staticmethod
    def _scan_filenames(output_dir: str) -> frozenset[str]:
        """Read existing regular filenames once for transfer and Source preflight."""
        names: set[str] = set()
        with os.scandir(output_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        names.add(os.path.normcase(entry.name))
                except OSError:
                    continue
        return frozenset(names)
