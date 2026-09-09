#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: run_reporting.py
# @Time    : 2026/9/2
# @Author  : Kevin
# @Describe: Terminal, human log, and durable JSON reporting for GeoAcquire runs.

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from geoacquire.core.config import ReportingConfig
from geoacquire.core.models import AcquisitionReport, DownloadRequest, Region
from geoacquire.core.run_state import RunStateTracker


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return 'unknown'
    total = max(0, int(round(seconds)))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f'{days}d {hours:02d}h {minutes:02d}m'
    if hours:
        return f'{hours}h {minutes:02d}m {secs:02d}s'
    if minutes:
        return f'{minutes}m {secs:02d}s'
    return f'{secs}s'


class RunReportingService:
    """Render one state model at three deliberately different detail levels.

    Terminal output is concise and is copied byte-for-byte into the human log.
    The log additionally contains one line per final TIF and per final error.
    JSON stores the complete request/TIF dependency graph and mutable run state.
    """

    def __init__(self, workspace_dir: str, config: ReportingConfig) -> None:
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.config = config
        self.json_path = self._resolve(config.json_path)
        self.log_path = self._resolve(config.log_path)
        if (
            self.json_path is not None
            and self.log_path is not None
            and os.path.normcase(self.json_path) == os.path.normcase(self.log_path)
        ):
            raise ValueError('reporting.json_path and reporting.log_path must be different files')
        previous = self._read_previous()
        self.state = RunStateTracker(previous)
        self._output_lock = Lock()
        self._snapshot_lock = Lock()
        self._directory_lock = Lock()
        self._failure_lock = Lock()
        self._directory_snapshots: dict[str, frozenset[str]] = {}
        self._logged_tifs: set[tuple[str, str]] = set()
        self._failure_recorded = False
        self._logger = self._build_logger()

    def _resolve(self, path: str | None) -> str | None:
        if path is None:
            return None
        return os.path.abspath(path if os.path.isabs(path) else os.path.join(self.workspace_dir, path))

    def _read_previous(self) -> dict[str, Any] | None:
        if self.json_path is None or self.config.overwrite or not os.path.isfile(self.json_path):
            return None
        try:
            with open(self.json_path, 'r', encoding='utf-8') as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f'Cannot resume reporting JSON {self.json_path}: {exc}. '
                'Repair it, choose another path, or set reporting.overwrite=true'
            ) from exc
        if payload.get('schema_version') != RunStateTracker.SCHEMA_VERSION:
            raise ValueError(
                f'Unsupported reporting JSON schema in {self.json_path}; '
                'choose another path or set reporting.overwrite=true'
            )
        return payload

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f'geoacquire.run.{id(self)}')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if self.log_path is not None:
            os.makedirs(os.path.dirname(self.log_path) or '.', exist_ok=True)
            mode = 'w' if self.config.overwrite else 'a'
            handler = logging.FileHandler(self.log_path, mode=mode, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
        return logger

    def scan_filenames(self, directory: str) -> frozenset[str]:
        """Return one cached regular-file name snapshot for recovery checks."""
        resolved = os.path.abspath(directory)
        with self._directory_lock:
            cached = self._directory_snapshots.get(resolved)
            if cached is not None:
                return cached
            names: set[str] = set()
            try:
                with os.scandir(resolved) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file():
                                names.add(os.path.normcase(entry.name))
                        except OSError:
                            continue
            except OSError:
                pass
            snapshot = frozenset(names)
            self._directory_snapshots[resolved] = snapshot
            return snapshot

    def start_pipeline(
        self,
        label: str,
        regions: Mapping[str, Region],
        expected_outputs: Mapping[str, Iterable[str]],
        existing_region_ids: Iterable[str],
        max_workers: int,
    ) -> None:
        self.state.start_pipeline(label, regions, expected_outputs, existing_region_ids, max_workers)
        for region_id in existing_region_ids:
            item = self.state.tif_state(label, region_id)
            self._log_tif(label, item)
        summary = self.state.summary(label)
        tifs = summary['tifs']
        self.terminal(
            'plan',
            f'TIF files: total={tifs["total"]}, already complete={tifs["existing"]}, '
            f'need processing={tifs["total"] - tifs["existing"]}; download threads={max_workers}',
        )
        self.save_json('run_started')

    def record_request(
        self,
        label: str,
        key: str,
        request: DownloadRequest,
        duplicate: bool = False,
        partial_file_found: bool = False,
    ) -> None:
        self.state.record_request(label, key, request, duplicate, partial_file_found)
        if duplicate:
            self.detail(
                'plan/duplicate',
                f'pipeline={label} destination={request.filename} action=reuse_first_destination',
            )

    def planning_issue(self, label: str, issue: Mapping[str, Any]) -> None:
        self.state.add_planning_issue(label, issue)
        self.detail(
            'plan/issue',
            f'pipeline={label} workunit={issue.get("workunit") or "unknown"} '
            f'status={issue.get("status") or "unknown"} error={issue.get("error") or "none"}',
            level=logging.WARNING,
        )
        self.save_json('planning_issue')

    def plan_finished(self, label: str) -> None:
        summary = self.state.finish_plan(label)
        if summary['planning_issue_count']:
            self.detail(
                'plan/summary',
                f'pipeline={label} selected_laz={summary["laz"]["total"]} '
                f'warnings={summary["planning_issue_count"]} '
                f'duplicate_destinations={summary["duplicate_destinations"]}',
                level=logging.WARNING,
            )
        self.save_json('plan_complete')

    def worker_started(self, label: str, key: str) -> None:
        self.state.worker_started(label, key)

    def transfer_finished(
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
        self.state.record_transfer(
            label, key, status, path, attempts, elapsed_seconds,
            resumed_from_bytes, bytes_on_disk, error,
        )

    def materialize_finished(self, label: str, key: str, elapsed_seconds: float) -> None:
        self.state.record_materialize(label, key, elapsed_seconds)

    def file_finished(
        self,
        label: str,
        key: str,
        filename: str,
        target_region_ids: Iterable[str],
        report: AcquisitionReport,
    ) -> None:
        self.state.file_finished(label, key, report)
        self._remember_report_files(report)
        failures = [failure for result in report.regions.values() for failure in result.failed]
        if failures:
            error = '; '.join(failure.error for failure in failures)
            self.detail(
                'laz/failed',
                f'pipeline={label} file={filename} affected_tifs={list(target_region_ids)} error={error}',
                level=logging.ERROR,
            )
            self.save_json('laz_error')

    def tif_ready(self, label: str, region_id: str) -> None:
        self.state.tif_ready(label, region_id)

    def tif_started(self, label: str, region_id: str) -> None:
        self.state.tif_started(label, region_id)

    def tif_finished(self, label: str, region_id: str, report: AcquisitionReport) -> None:
        item, milestones, summary = self.state.tif_finished(label, region_id, report)
        self._remember_report_files(report)
        self._log_tif(label, item)
        if item['status'] == 'failed':
            self.save_json('tif_error')
        for milestone in milestones:
            self._progress_line(milestone, summary)
            self.save_json(f'{milestone}_percent')

    def finish_pipeline(self, label: str) -> dict[str, Any]:
        summary = self.state.finish_pipeline(label)
        snapshot = self.state.snapshot()['pipelines'][label]
        for item in snapshot['tifs'].values():
            self._log_tif(label, item)
        tifs = summary['tifs']
        laz = summary['laz']
        self.terminal(
            'final',
            f'TIF files: complete={tifs["complete"]}, already complete={tifs["existing"]}, '
            f'failed={tifs["failed"]}, unavailable={tifs["unavailable"]}, total={tifs["total"]}',
        )
        self.terminal(
            'final',
            f'LAZ files: downloaded={laz["downloaded_this_run"]}, already available={laz["already_available"]}, '
            f'failed={laz["failed"]}, total={laz["total"]}',
        )
        self.terminal(
            'final',
            f'Elapsed={_duration(summary["elapsed_seconds"])}, '
            f'average active download threads while busy={summary["workers"]["average_active"]}/'
            f'{summary["workers"]["configured"]}, planning warnings={summary["planning_issue_count"]}',
        )
        self.save_json('pipeline_final')
        return summary

    def complete_run(self) -> None:
        self.state.complete_run()
        self.save_json('run_complete')

    def fail_run(self, error: BaseException | str, interrupted: bool = False) -> None:
        with self._failure_lock:
            if self._failure_recorded:
                return
            self._failure_recorded = True
        message = str(error)
        self.state.fail_run(message, interrupted)
        self.terminal('interrupted' if interrupted else 'error', message)
        self.save_json('interrupted' if interrupted else 'run_error')

    def detail(self, event: str, message: str, level: int = logging.INFO) -> None:
        line = f'{_timestamp()} [{event}] {message}'
        self._logger.log(level, line)

    def terminal(self, event: str, message: str) -> None:
        """Emit one concise terminal line and mirror the exact line into the log."""
        line = f'{_timestamp()} [{event}] {message}'
        with self._output_lock:
            print(line, flush=True)
            self._logger.info(line)

    def save_json(self, reason: str) -> None:
        if self.json_path is None:
            return
        with self._snapshot_lock:
            payload = self.state.snapshot()
            payload['last_checkpoint'] = {'timestamp': _timestamp(), 'reason': reason}
            os.makedirs(os.path.dirname(self.json_path) or '.', exist_ok=True)
            temporary = self.json_path + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
            os.replace(temporary, self.json_path)

    def close(self) -> None:
        handlers = list(self._logger.handlers)
        for handler in handlers:
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)

    def _progress_line(self, milestone: int, summary: Mapping[str, Any]) -> None:
        tifs = summary['tifs']
        laz = summary['laz']
        workers = summary['workers']
        self.terminal(
            'progress',
            f'{milestone}% TIF complete; TIF files complete={tifs["complete"] + tifs["existing"]}/'
            f'{tifs["total"]}, remaining={tifs["remaining"]}, failed={tifs["failed"]}; '
            f'LAZ files discovered={laz["total"]}, downloaded={laz["downloaded_this_run"]}, '
            f'already available={laz["already_available"]}, failed tasks={laz["failed"]}, '
            f'running tasks={laz["running"]}; download threads active={workers["active"]}/'
            f'{workers["configured"]}, average while busy since last update='
            f'{workers.get("average_active_since_checkpoint", 0)}; '
            f'elapsed={_duration(summary["elapsed_seconds"])}, '
            f'estimated remaining={_duration(summary["estimated_remaining_seconds"])}',
        )

    def _log_tif(self, label: str, item: Mapping[str, Any]) -> None:
        key = (label, str(item['region_id']))
        if key in self._logged_tifs or item['status'] not in {'complete', 'existing', 'failed', 'unavailable'}:
            return
        self._logged_tifs.add(key)
        outputs = item.get('output_paths') or []
        timings = item['timings']
        event = {
            'complete': 'tif/success',
            'existing': 'tif/existing',
            'failed': 'tif/failed',
            'unavailable': 'tif/unavailable',
        }[item['status']]
        message = (
            f'pipeline={label} input={item.get("input_path")} outputs={outputs} '
            f'laz_files={len(item.get("required_laz") or [])} '
            f'wait_for_laz={_duration(timings.get("wait_for_laz_seconds"))} '
            f'build_tif={_duration(timings.get("build_tif_seconds"))} '
            f'total={_duration(timings.get("total_seconds"))}'
        )
        if item.get('error'):
            message += f' error={item["error"]}'
        if item.get('failed_laz'):
            message += f' failed_laz={item["failed_laz"]}'
        level = (
            logging.ERROR
            if item['status'] == 'failed'
            else logging.WARNING if item['status'] == 'unavailable' else logging.INFO
        )
        self.detail(event, message, level)

    def _remember_report_files(self, report: AcquisitionReport) -> None:
        """Keep cached directory snapshots correct after current-run outputs appear."""
        with self._directory_lock:
            for _, asset in report.iter_assets():
                path = os.path.abspath(asset.path)
                directory = os.path.dirname(path)
                cached = self._directory_snapshots.get(directory)
                if cached is None:
                    continue
                self._directory_snapshots[directory] = frozenset({
                    *cached,
                    os.path.normcase(os.path.basename(path)),
                })
