#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: sample_lidar_headers.py
# @Time    : 2026/8/31
# @Author  : Kevin
# @Describe: Explicit, bounded header sampling for maintainers; never used during downloads.

import argparse
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import laspy
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.replay_lidar_audit import CachedAuditSource, load_workunits


def sample_header(item: dict) -> dict:
    result = dict(item)
    result['url'] = CachedAuditSource.normalize_link(item['url'])
    try:
        # stream=True is essential: a server ignoring Range must not cause a
        # full LAZ download. Read at most 512 KiB, including VLR CRS metadata.
        with requests.get(result['url'], headers={'Range': 'bytes=0-524287'}, stream=True, timeout=(15, 45)) as response:
            response.raise_for_status()
            payload = response.raw.read(524288)
        # EVLRs live at the end of LAS 1.4 files, outside this bounded prefix.
        # Keep the bounds even when the CRS is only in an unavailable EVLR.
        with laspy.open(io.BytesIO(payload), read_evlrs=False) as reader:
            header = reader.header
            result['bounds'] = [float(value) for value in (*header.mins[:2], *header.maxs[:2])]
            crs = header.parse_crs()
            result['header_crs'] = crs.to_string() if crs else None
    except Exception as exc:
        result['error'] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description='Sample official LAZ headers to review, not automatically infer, rules.')
    parser.add_argument('--audit', required=True)
    parser.add_argument('--states', nargs='+')
    parser.add_argument('--workunit', help='Optional workunit regular expression.')
    parser.add_argument('--filename', help='Optional filename regular expression for rare/boundary names.')
    parser.add_argument('--priority', action='store_true', help='Only newest readable workunit for each target.')
    parser.add_argument('--per-project', action='store_true', help='One workunit per project/CRS for an initial survey only.')
    parser.add_argument('--cache', default='cache/link_lists')
    parser.add_argument('--report', default='logs/lidar_header_samples.json')
    parser.add_argument('--retry-errors', action='store_true', help='Retry only failed entries already in --report.')
    args = parser.parse_args()
    source = CachedAuditSource(link_cache_dir=args.cache)
    items = [] if args.retry_errors else load_workunits(args.audit, args.states)
    if args.priority:
        seen = set()
        chosen = []
        for item in items:
            if item['filename_count'] and set(item['targets']) - seen:
                chosen.append(item)
                seen.update(item['targets'])
        items = chosen
    if args.workunit:
        items = [item for item in items if re.search(args.workunit, item['workunit'], re.I)]
    jobs = []
    seen = set()
    for item in items:
        key = (item['project'], item['horiz_crs'])
        if args.per_project and key in seen:
            continue
        seen.add(key)
        try:
            text = source._fetch_text(item['lpc_link'] + '/0_file_download_links.txt')
        except OSError:
            continue
        urls = sorted({line.strip() for line in text.splitlines() if line.lower().endswith(('.laz', '.las'))})
        urls = [url for url in urls if not source._is_project_placeholder(os.path.basename(urlparse(url).path))]
        if args.filename:
            urls = [url for url in urls if re.search(args.filename, os.path.basename(urlparse(url).path), re.I)]
        if not urls:
            continue
        for index in sorted({0, len(urls) // 2, len(urls) - 1}):
            jobs.append({key: item[key] for key in ('workunit', 'project', 'horiz_crs')} | {'url': urls[index]})
    results = []
    if args.retry_errors:
        with open(args.report, 'r', encoding='utf-8') as file:
            previous = json.load(file)
        results = [item for item in previous if 'error' not in item]
        jobs = [{key: value for key, value in item.items() if key != 'error'} for item in previous if 'error' in item]
    print(f'[headers] samples={len(jobs)}; maximum 512 KiB each', flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(sample_header, jobs):
            results.append(result)
            print(result['workunit'], os.path.basename(result['url']), result.get('bounds', result.get('error')), flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
    with open(args.report, 'w', encoding='utf-8') as file:
        json.dump(results, file, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
