#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: replay_lidar_audit.py
# @Time    : 2026/8/31
# @Author  : Kevin
# @Describe: Replay recorded target bounds against cached link lists, without network I/O.

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely.geometry import GeometryCollection, box

from geoacquire.core.models import Region
from geoacquire.sources.usgs.lidar import USGSLidarSource


class CachedAuditSource(USGSLidarSource):
    """A maintenance-only Source that cannot refresh link lists over HTTP."""

    def _fetch_text(self, url: str) -> str:
        with open(self.link_cache_path(url), 'r', encoding='utf-8') as file:
            return file.read()


def load_workunits(path: str, states: list[str] | None = None) -> list[dict]:
    """Merge repeated audit rows while retaining every recorded target."""
    with open(path, 'r', encoding='utf-8') as file:
        audit = json.load(file)
    unique = {}
    for item in audit:
        targets = {
            target['region_id']: target for target in item['targets']
            if not states or target['region_id'].split('__')[0] in states
        }
        if not targets:
            continue
        key = (item['lpc_link'], item['workunit'])
        if key not in unique:
            unique[key] = {**item, 'targets': {}}
        unique[key]['targets'].update(targets)
    return sorted(unique.values(), key=lambda item: str(item.get('collect_end') or ''), reverse=True)


def replay(audit: str, source: CachedAuditSource, states=None, coverage=False) -> dict:
    items = load_workunits(audit, states)
    targets = {key: target for item in items for key, target in item['targets'].items()}
    regions = {
        key: Region(key, tuple(target['bounds']), target['crs'], {})
        for key, target in targets.items()
    }
    covered = {key: GeometryCollection() for key in regions}
    rows = []
    selected_files = set()
    for index, item in enumerate(items, 1):
        plan = source._load_workunit_plan(item)
        status = 'matched' if plan.tiles and not plan.unmatched else 'partial' if plan.tiles else 'unmatched'
        row = {
            'workunit': item['workunit'], 'project': item['project'],
            'collect_end': item.get('collect_end'), 'horiz_crs': item.get('horiz_crs'),
            'lpc_link': item['lpc_link'], 'rule': plan.rule_name, 'status': status,
            'filename_count': plan.filenames, 'parsed_count': len(plan.tiles),
            'unmatched_count': len(plan.unmatched), 'filename_examples': list(plan.unmatched[:10]),
            'ignored_placeholder_count': plan.ignored_placeholders, 'error': plan.error,
            'target_ids': sorted(item['targets']),
        }
        if coverage and plan.tiles:
            selected = source._select_tiles(plan.tiles, set(item['targets']), regions, covered)
            row['selected_count'] = len(selected)
            selected_files.update(tile.filename for tile, _ in selected.values())
        rows.append(row)
        if index % 25 == 0 or index == len(items):
            print(f'[replay] workunits={index}/{len(items)}', flush=True)
        # Replay consumes each list once. Do not retain a nationwide LAZ index.
        source._workunit_cache.clear()

    state_summary = {}
    incomplete = []
    for key, region in regions.items():
        state = key.split('__')[0]
        entry = state_summary.setdefault(state, {'targets': 0, 'covered': 0 if coverage else None})
        entry['targets'] += 1
        if coverage:
            target_shape = box(*region.bounds)
            ratio = min(1.0, covered[key].intersection(target_shape).area / target_shape.area)
            if ratio >= 1 - 1e-6:
                entry['covered'] += 1
            else:
                incomplete.append({
                    'region_id': key, 'coverage': round(ratio, 6),
                    'bounds': list(region.bounds), 'crs': region.crs,
                    'target_path': targets[key].get('target_path'),
                    'workunits': [item['workunit'] for item in items if key in item['targets']],
                })
    return {
        'audit': os.path.abspath(audit), 'catalog': source.catalog.path,
        'coverage_checked': coverage,
        'note': 'Filename footprints only; no point-density, raster validity or vertical-datum check.',
        'summary': dict(Counter(row['status'] for row in rows)),
        'states': dict(sorted(state_summary.items())),
        'unique_selected_files': len(selected_files) if coverage else None,
        'incomplete_targets': incomplete if coverage else None, 'workunits': rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', required=True)
    parser.add_argument('--catalog', default='configs/usgs_lidar_projects.yaml')
    parser.add_argument('--cache', default='cache/link_lists')
    parser.add_argument('--states', nargs='+', help='Recorded ID prefixes, for example ME IL.')
    parser.add_argument('--coverage', action='store_true', help='Also replay footprint selection on recorded bounds.')
    parser.add_argument('--report', default='logs/lidar_audit_replay.json')
    args = parser.parse_args()
    if os.path.abspath(args.report) == os.path.abspath(args.audit):
        parser.error('--report must not overwrite the input audit')
    source = CachedAuditSource(project_catalog_path=args.catalog, link_cache_dir=args.cache)
    result = replay(args.audit, source, args.states, args.coverage)
    os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
    with open(args.report, 'w', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(json.dumps({key: result[key] for key in ('summary', 'states', 'unique_selected_files')}, indent=2))
    print(f'[replay] report={os.path.abspath(args.report)}')


if __name__ == '__main__':
    main()
