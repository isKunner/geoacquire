#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: run.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: class_path-driven GeoAcquire command-line entry point.

import argparse
import os
import warnings

from geoacquire.core.config import load_config
from geoacquire.core.context import RuntimeContext
from geoacquire.core.pipeline import PipelineRunner, format_region_ids
from geoacquire.services.http_download import HTTPDownloadService
from geoacquire.services.run_reporting import RunReportingService

warnings.filterwarnings('ignore', message=".*doesn't match a supported version.*")


def check_config(config) -> None:
    regions = config.region.get_regions()
    print(f'[check] region: {type(config.region).__module__}.{type(config.region).__name__}')
    print(f'[check] resolved regions: {format_region_ids(regions)}')
    print(
        f'[check] reporting: log_path={config.reporting.log_path} '
        f'json_path={config.reporting.json_path} overwrite={config.reporting.overwrite}'
    )
    for index, pipeline in enumerate(config.pipelines):
        source = f'{type(pipeline.source).__module__}.{type(pipeline.source).__name__}'
        processors = [f'{type(item).__module__}.{type(item).__name__}' for item in pipeline.postprocess]
        declared = pipeline.source.output_specs
        assets = 'unknown' if declared is None else sorted(f'{item.kind}:{item.product}' for item in declared)
        print(
            f'[check] pipelines[{index}]: name={pipeline.name} enable={pipeline.enable} source={source} '
            f'assets={assets} postprocess={processors}'
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Acquire geospatial source data from class_path-based YAML pipelines.'
    )
    parser.add_argument('-c', '--config', nargs='+', required=True, help='YAML files merged in order')
    # --set allows overriding YAML values from the command line.
    #   dest='overrides'    -> the parsed value is stored as args.overrides, not args.set.
    #   action='append'     -> the flag can be repeated; each occurrence is appended to a list.
    #   default=[]          -> when --set is never used, the default is an empty list.
    #   metavar='PATH=VALUE'-> shown in --help to indicate the expected format.
    parser.add_argument(
        '--set',
        dest='overrides',
        action='append',
        default=[],
        metavar='PATH=VALUE',
        help='override one existing YAML value; repeat for multiple values',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='instantiate and validate configuration without acquisition',
    )
    args = parser.parse_args()

    config_paths = [os.path.abspath(path) for path in args.config]
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)
    config = load_config(config_paths, args.overrides)
    if args.check:
        check_config(config)
        return

    reporting = RunReportingService(project_root, config.reporting)
    context = RuntimeContext(
        workspace_dir=project_root,
        http=HTTPDownloadService(),
        reporting=reporting,
    )
    try:
        PipelineRunner(context).run(config.region, config.pipelines)
    except KeyboardInterrupt:
        reporting.fail_run('Interrupted by user', interrupted=True)
        raise
    except Exception as exc:
        reporting.fail_run(exc)
        raise
    finally:
        reporting.close()


if __name__ == '__main__':
    main()
