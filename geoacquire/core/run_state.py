#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: run_state.py
# @Time    : 2026/9/2
# @Author  : Kevin
# @Describe: Thread-safe in-memory state for one observable GeoAcquire run.

from __future__ import annotations

import copy
import os
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from .models import AcquisitionReport, DownloadRequest, Region


_FINAL_TIF_STATES = frozenset({'complete', 'existing', 'failed', 'unavailable'})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _json_value(value: Any) -> Any:
    """Copy nested metadata into JSON-safe scalar/list/mapping values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return str(value)


def _seconds(value: float) -> float:
    return round(max(0.0, value), 3)


class RunStateTracker:
    """Own the complete mutable run model without doing terminal or file I/O.

    Every update is small and protected by one re-entrant lock. Expensive JSON
    serialization is performed on a detached snapshot after the lock is released.
    Worker utilization is integrated only when a worker changes state, so no
    polling thread or synchronization barrier is required.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        previous: dict[str, Any] | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = RLock()
        self._previous = copy.deepcopy(previous) if previous else None
        now = self._monotonic()
        previous_errors = [
            *((previous or {}).get('previous_errors') or []),
            *((previous or {}).get('errors') or []),
        ]
        self._state: dict[str, Any] = {
            'schema_version': self.SCHEMA_VERSION,
            'status': 'running',
            'started_at': self._wall_clock(),
            'updated_at': self._wall_clock(),
            'resume_count': int((previous or {}).get('resume_count') or 0) + bool(previous),
            'pipelines': {},
            'errors': [],
            'previous_errors': previous_errors,
        }
        self._runtime: dict[str, dict[str, Any]] = {}

    def start_pipeline(
        self,
        label: str,
        regions: Mapping[str, Region],
        expected_outputs: Mapping[str, Iterable[str]],
        existing_region_ids: Iterable[str],
        max_workers: int,
    ) -> None:
        """Initialize one pipeline from the disk-derived final-output snapshot."""
        with self._lock:
            existing = set(existing_region_ids)
            previous_pipeline = ((self._previous or {}).get('pipelines') or {}).get(label)
            current_inputs = {
                region_id: os.path.abspath(region.target_path) if region.target_path else None
                for region_id, region in regions.items()
            }
            if previous_pipeline:
                previous_inputs = {
                    region_id: item.get('input_path')
                    for region_id, item in (previous_pipeline.get('tifs') or {}).items()
                }
                if previous_inputs and previous_inputs != current_inputs:
                    raise ValueError(
                        'Existing reporting JSON belongs to different input TIF files; '
                        'choose another reporting.json_path or set reporting.overwrite=true'
                    )

            now = self._monotonic()
            old_tifs = (previous_pipeline or {}).get('tifs') or {}
            tifs: dict[str, dict[str, Any]] = {}
            for region_id, region in regions.items():
                status = 'existing' if region_id in existing else 'pending'
                item = {
                    'region_id': region_id,
                    'input_path': current_inputs[region_id],
                    'output_paths': [
                        os.path.abspath(path)
                        for path in expected_outputs.get(region_id, ())
                    ],
                    'status': status,
                    'required_laz': [],
                    'completed_laz': [],
                    'failed_laz': [],
                    'timestamps': {
                        'ready_at': None,
                        'build_started_at': None,
                        'finished_at': self._wall_clock() if status == 'existing' else None,
                    },
                    'timings': {
                        'wait_for_laz_seconds': 0.0,
                        'build_tif_seconds': 0.0,
                        'total_seconds': 0.0,
                    },
                    'error': None,
                }
                if region_id in old_tifs:
                    item['previous_status'] = old_tifs[region_id].get('status')
                    if status == 'existing':
                        item['required_laz'] = list(old_tifs[region_id].get('required_laz') or [])
                        item['completed_laz'] = list(old_tifs[region_id].get('completed_laz') or [])
                tifs[region_id] = item

            historical_laz_files: dict[str, Any] = {}
            if previous_pipeline:
                for key, item in (previous_pipeline.get('laz_files') or {}).items():
                    if any(region_id in existing for region_id in item.get('target_region_ids') or []):
                        historical_laz_files[key] = copy.deepcopy(item)
            self._state['pipelines'][label] = {
                'name': label,
                'status': 'planning',
                'started_at': self._wall_clock(),
                'finished_at': None,
                'max_workers': max_workers,
                'request_stream_seconds': 0.0,
                'duplicate_destinations': 0,
                'planning_issues': [],
                'tifs': tifs,
                'laz_files': {},
                # Completed final TIFs bypass Source planning. Retain their previous
                # dependency records without counting them as current-run LAZ work.
                'historical_laz_files': historical_laz_files,
                'summary': {},
            }
            total_tifs = len(tifs)
            initial_complete = len(existing)
            self._runtime[label] = {
                'pipeline_started': now,
                'plan_finished': None,
                'active_workers': 0,
                'worker_area': 0.0,
                'worker_busy_time': 0.0,
                'worker_last_change': now,
                'checkpoint_area': 0.0,
                'checkpoint_busy_time': 0.0,
                'next_milestone': (
                    min(11, initial_complete * 10 // total_tifs + 1)
                    if total_tifs
                    else 11
                ),
                'tif_build_started': {},
            }
            self._touch()

    def record_request(
        self,
        label: str,
        key: str,
        request: DownloadRequest,
        duplicate: bool = False,
        partial_file_found: bool = False,
    ) -> None:
        """Record one selected physical file and merge all dependent TIF IDs."""
        with self._lock:
            pipeline = self._pipeline(label)
            files = pipeline['laz_files']
            file_state = files.get(key)
            if file_state is None:
                old = (((self._previous or {}).get('pipelines') or {}).get(label) or {}).get('laz_files', {}).get(key)
                file_state = {
                    'key': key,
                    'filename': request.filename,
                    'asset_id': request.asset_id,
                    'url': request.url,
                    'kind': str(request.kind),
                    'product': str(request.product),
                    'target_region_ids': [],
                    'metadata': _json_value(request.metadata),
                    'header_names': sorted(request.headers),
                    'duplicate_requests': [],
                    'status': 'planned',
                    'path': None,
                    'partial_file_found': False,
                    'download': {
                        'status': 'pending',
                        'attempts': 0,
                        'elapsed_seconds': 0.0,
                        'resumed_from_bytes': 0,
                        'bytes_on_disk': 0,
                    },
                    'materialize': {'elapsed_seconds': 0.0},
                    'worker_seconds': 0.0,
                    'timestamps': {
                        'planned_at': self._wall_clock(),
                        'started_at': None,
                        'finished_at': None,
                    },
                    'error': None,
                }
                if old:
                    file_state['previous_status'] = old.get('status')
                    file_state['previous_error'] = old.get('error')
                files[key] = file_state
            elif duplicate:
                pipeline['duplicate_destinations'] += 1
                file_state['duplicate_requests'].append({
                    'asset_id': request.asset_id,
                    'url': request.url,
                    'target_region_ids': list(request.target_region_ids),
                    'metadata': _json_value(request.metadata),
                })

            target_ids = file_state['target_region_ids']
            file_state['partial_file_found'] = file_state['partial_file_found'] or partial_file_found
            for region_id in request.target_region_ids:
                if region_id not in target_ids:
                    target_ids.append(region_id)
                tif = pipeline['tifs'].get(region_id)
                if tif is not None and key not in tif['required_laz']:
                    tif['required_laz'].append(key)
                    if file_state['status'] in {'complete', 'existing'}:
                        tif['completed_laz'].append(key)
                    elif file_state['status'] == 'failed':
                        tif['failed_laz'].append(key)
            self._touch()

    def add_planning_issue(self, label: str, issue: Mapping[str, Any]) -> None:
        with self._lock:
            normalized = _json_value(issue)
            self._pipeline(label)['planning_issues'].append(normalized)
            self._state['errors'].append({
                'timestamp': self._wall_clock(),
                'category': 'planning',
                'pipeline': label,
                'details': normalized,
            })
            self._touch()

    def finish_plan(self, label: str) -> dict[str, Any]:
        """Mark the end of the streamed planner; only now is the LAZ total exact."""
        with self._lock:
            pipeline = self._pipeline(label)
            runtime = self._runtime[label]
            now = self._monotonic()
            runtime['plan_finished'] = now
            pipeline['request_stream_seconds'] = _seconds(now - runtime['pipeline_started'])
            pipeline['status'] = 'running'
            summary = self._summary_locked(label, now)
            pipeline['summary'] = summary
            self._touch()
            return copy.deepcopy(summary)

    def worker_started(self, label: str, key: str) -> None:
        with self._lock:
            now = self._monotonic()
            self._update_worker_area(label, now)
            runtime = self._runtime[label]
            runtime['active_workers'] += 1
            item = self._pipeline(label)['laz_files'][key]
            item['status'] = 'running'
            item['timestamps']['started_at'] = self._wall_clock()
            item['_worker_started'] = now
            self._touch()

    def record_transfer(
        self,
        label: str,
        key: str,
        status: str,
        path: str,
        attempts: int,
        elapsed_seconds: float,
        resumed_from_bytes: int = 0,
        bytes_on_disk: int = 0,
        error: str | None = None,
    ) -> None:
        with self._lock:
            item = self._pipeline(label)['laz_files'][key]
            item['path'] = os.path.abspath(path)
            item['download'].update({
                'status': status,
                'attempts': attempts,
                'elapsed_seconds': _seconds(elapsed_seconds),
                'resumed_from_bytes': resumed_from_bytes,
                'bytes_on_disk': bytes_on_disk,
            })
            if error:
                item['error'] = error
            self._touch()

    def record_materialize(self, label: str, key: str, elapsed_seconds: float) -> None:
        with self._lock:
            self._pipeline(label)['laz_files'][key]['materialize']['elapsed_seconds'] = _seconds(elapsed_seconds)
            self._touch()

    def file_finished(
        self,
        label: str,
        key: str,
        report: AcquisitionReport,
    ) -> dict[str, Any]:
        """Close one worker task and update every dependent TIF dependency list."""
        with self._lock:
            now = self._monotonic()
            self._update_worker_area(label, now)
            runtime = self._runtime[label]
            runtime['active_workers'] = max(0, runtime['active_workers'] - 1)
            item = self._pipeline(label)['laz_files'][key]
            started = item.pop('_worker_started', now)
            item['worker_seconds'] = _seconds(now - started)
            failures = [failure for result in report.regions.values() for failure in result.failed]
            item['status'] = 'failed' if failures else (
                'existing' if item['download']['status'] == 'existing' else 'complete'
            )
            item['timestamps']['finished_at'] = self._wall_clock()
            if failures:
                item['error'] = '; '.join(failure.error for failure in failures)
                self._state['errors'].append({
                    'timestamp': self._wall_clock(),
                    'category': 'laz',
                    'pipeline': label,
                    'filename': item['filename'],
                    'target_region_ids': list(item['target_region_ids']),
                    'error': item['error'],
                })
            for region_id in item['target_region_ids']:
                tif = self._pipeline(label)['tifs'][region_id]
                collection = tif['failed_laz'] if failures else tif['completed_laz']
                if key not in collection:
                    collection.append(key)

            summary = self._summary_locked(label, now)
            self._pipeline(label)['summary'] = summary
            self._touch()
            return copy.deepcopy(summary)

    def tif_ready(self, label: str, region_id: str) -> None:
        with self._lock:
            now = self._monotonic()
            pipeline = self._pipeline(label)
            tif = pipeline['tifs'][region_id]
            if tif['status'] in _FINAL_TIF_STATES:
                return
            tif['status'] = 'ready'
            tif['timestamps']['ready_at'] = self._wall_clock()
            start = self._runtime[label]['pipeline_started']
            tif['timings']['wait_for_laz_seconds'] = _seconds(now - start)
            self._touch()

    def tif_started(self, label: str, region_id: str) -> None:
        with self._lock:
            now = self._monotonic()
            tif = self._pipeline(label)['tifs'][region_id]
            tif['status'] = 'building'
            tif['timestamps']['build_started_at'] = self._wall_clock()
            self._runtime[label]['tif_build_started'][region_id] = now
            self._touch()

    def tif_finished(
        self,
        label: str,
        region_id: str,
        report: AcquisitionReport,
    ) -> tuple[dict[str, Any], list[int], dict[str, Any]]:
        with self._lock:
            now = self._monotonic()
            tif = self._pipeline(label)['tifs'][region_id]
            started = self._runtime[label]['tif_build_started'].pop(region_id, now)
            build_seconds = now - started
            failures = report.regions[region_id].failed
            produced_paths = {
                os.path.normcase(os.path.abspath(asset.path))
                for asset in report.regions[region_id].usable()
            }
            missing_outputs = [
                path
                for path in tif['output_paths']
                if os.path.normcase(os.path.abspath(path)) not in produced_paths
            ]
            tif['status'] = 'failed' if failures or missing_outputs else 'complete'
            tif['timestamps']['finished_at'] = self._wall_clock()
            tif['timings']['build_tif_seconds'] = _seconds(build_seconds)
            tif['timings']['total_seconds'] = _seconds(
                tif['timings']['wait_for_laz_seconds'] + build_seconds
            )
            if failures:
                tif['error'] = '; '.join(failure.error for failure in failures)
            elif missing_outputs:
                tif['error'] = f'Expected final output was not produced: {missing_outputs}'
            else:
                tif['error'] = None
            if tif['error']:
                self._state['errors'].append({
                    'timestamp': self._wall_clock(),
                    'category': 'tif',
                    'pipeline': label,
                    'input_path': tif['input_path'],
                    'output_paths': list(tif['output_paths']),
                    'error': tif['error'],
                })
            summary = self._summary_locked(label, now)
            total = summary['tifs']['total']
            completed = summary['tifs']['complete'] + summary['tifs']['existing']
            reached = completed * 10 // total if total else 10
            runtime = self._runtime[label]
            milestones: list[int] = []
            if runtime['next_milestone'] <= min(10, reached):
                # One TIF can cross several deciles in a small run. Emit one
                # concise checkpoint at the highest newly reached percentage.
                milestones.append(min(10, reached) * 10)
                runtime['next_milestone'] = min(10, reached) + 1
            if milestones:
                summary['workers']['average_active_since_checkpoint'] = self._checkpoint_average(label)
            self._pipeline(label)['summary'] = summary
            self._touch()
            return copy.deepcopy(tif), milestones, copy.deepcopy(summary)

    def finish_pipeline(self, label: str) -> dict[str, Any]:
        """Resolve targets that never became ready and produce the final summary."""
        with self._lock:
            now = self._monotonic()
            pipeline = self._pipeline(label)
            for tif in pipeline['tifs'].values():
                if tif['status'] in _FINAL_TIF_STATES:
                    continue
                if tif['failed_laz']:
                    tif['status'] = 'failed'
                    tif['error'] = f'{len(tif["failed_laz"])} required LAZ file(s) failed'
                else:
                    tif['status'] = 'unavailable'
                    tif['error'] = 'No usable source coverage produced a final TIF'
                self._state['errors'].append({
                    'timestamp': self._wall_clock(),
                    'category': 'tif',
                    'pipeline': label,
                    'input_path': tif['input_path'],
                    'output_paths': list(tif['output_paths']),
                    'error': tif['error'],
                })
                tif['timestamps']['finished_at'] = self._wall_clock()
                start = self._runtime[label]['pipeline_started']
                tif['timings']['total_seconds'] = _seconds(now - start)
            pipeline['status'] = 'failed' if any(
                item['status'] in {'failed', 'unavailable'} for item in pipeline['tifs'].values()
            ) else 'complete'
            pipeline['finished_at'] = self._wall_clock()
            summary = self._summary_locked(label, now)
            pipeline['summary'] = summary
            self._touch()
            return copy.deepcopy(summary)

    def fail_run(self, error: str, interrupted: bool = False) -> None:
        with self._lock:
            self._state['status'] = 'interrupted' if interrupted else 'failed'
            self._state['errors'].append({
                'timestamp': self._wall_clock(),
                'category': 'interrupted' if interrupted else 'run',
                'error': error,
                'interrupted': interrupted,
            })
            self._touch()

    def complete_run(self) -> None:
        with self._lock:
            self._state['status'] = 'complete'
            self._state['finished_at'] = self._wall_clock()
            self._touch()

    def summary(self, label: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._summary_locked(label, self._monotonic()))

    def snapshot(self) -> dict[str, Any]:
        """Detach a serialization snapshot without holding the lock during I/O."""
        with self._lock:
            snapshot = copy.deepcopy(self._state)
        for pipeline in snapshot.get('pipelines', {}).values():
            for item in pipeline.get('laz_files', {}).values():
                item.pop('_worker_started', None)
        return snapshot

    def tif_state(self, label: str, region_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._pipeline(label)['tifs'][region_id])

    def _pipeline(self, label: str) -> dict[str, Any]:
        try:
            return self._state['pipelines'][label]
        except KeyError as exc:
            raise RuntimeError(f'Reporting pipeline was not started: {label}') from exc

    def _touch(self) -> None:
        self._state['updated_at'] = self._wall_clock()

    def _update_worker_area(self, label: str, now: float) -> None:
        runtime = self._runtime[label]
        elapsed = max(0.0, now - runtime['worker_last_change'])
        runtime['worker_area'] += runtime['active_workers'] * elapsed
        if runtime['active_workers'] > 0:
            runtime['worker_busy_time'] += elapsed
        runtime['worker_last_change'] = now

    def _checkpoint_average(self, label: str) -> float:
        runtime = self._runtime[label]
        area = runtime['worker_area'] - runtime['checkpoint_area']
        busy_time = runtime['worker_busy_time'] - runtime['checkpoint_busy_time']
        average = area / busy_time if busy_time else float(runtime['active_workers'])
        runtime['checkpoint_area'] = runtime['worker_area']
        runtime['checkpoint_busy_time'] = runtime['worker_busy_time']
        return round(average, 2)

    def _summary_locked(self, label: str, now: float) -> dict[str, Any]:
        pipeline = self._pipeline(label)
        runtime = self._runtime[label]
        self._update_worker_area(label, now)
        tifs = list(pipeline['tifs'].values())
        files = list(pipeline['laz_files'].values())
        tif_counts = {
            status: sum(item['status'] == status for item in tifs)
            for status in ('existing', 'pending', 'ready', 'building', 'complete', 'failed', 'unavailable')
        }
        complete_files = sum(item['status'] in {'existing', 'complete'} for item in files)
        failed_files = sum(item['status'] == 'failed' for item in files)
        running_files = sum(item['status'] == 'running' for item in files)
        downloaded = sum(
            item['download']['status'] == 'success'
            for item in files
        )
        existing = sum(
            item['download']['status'] in {'existing', 'skipped'}
            for item in files
        )
        total = len(files)
        remaining_files = max(0, total - complete_files - failed_files)
        elapsed = max(0.0, now - runtime['pipeline_started'])
        completed_tifs_this_run = tif_counts['complete']
        remaining_tifs = sum(item['status'] not in _FINAL_TIF_STATES for item in tifs)
        rate = completed_tifs_this_run / elapsed if elapsed and completed_tifs_this_run else 0.0
        eta = remaining_tifs / rate if rate else None
        average_active = (
            runtime['worker_area'] / runtime['worker_busy_time']
            if runtime['worker_busy_time']
            else 0.0
        )
        return {
            'tifs': {
                'total': len(tifs),
                **tif_counts,
                'finished': sum(item['status'] in _FINAL_TIF_STATES for item in tifs),
                'in_progress': sum(item['status'] not in _FINAL_TIF_STATES for item in tifs),
                'remaining': len(tifs) - tif_counts['complete'] - tif_counts['existing'],
            },
            'laz': {
                'total': total,
                'planning_complete': runtime['plan_finished'] is not None,
                'already_available': existing,
                'need_download': max(0, total - existing),
                'downloaded_this_run': downloaded,
                'complete': complete_files,
                'failed': failed_files,
                'running': running_files,
                'remaining': remaining_files,
                'partial_files': sum(bool(item['partial_file_found']) for item in files),
            },
            'workers': {
                'configured': pipeline['max_workers'],
                'active': runtime['active_workers'],
                'average_active': round(average_active, 2),
            },
            'request_stream_seconds': pipeline['request_stream_seconds'],
            'elapsed_seconds': _seconds(now - runtime['pipeline_started']),
            'estimated_remaining_seconds': _seconds(eta) if eta is not None else None,
            'planning_issue_count': len(pipeline['planning_issues']),
            'duplicate_destinations': pipeline['duplicate_destinations'],
        }
