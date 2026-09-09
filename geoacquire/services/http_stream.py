#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: http_stream.py
# @Time    : 2026/8/31
# @Author  : Kevin
# @Describe: Shared byte-range validation and bounded streaming into partial files.

import os
import re

import requests


def write_response_body(
    response: requests.Response,
    part_path: str,
    resume_byte: int,
    chunk_size: int,
) -> int:
    """Write an already status-checked 200/206 response without publishing it.

    Callers request Accept-Encoding: identity and own authentication, retries,
    response closing and final os.replace. A 200 rewrites an ignored Range;
    a 206 must start exactly at resume_byte and complete the advertised file.
    Example: a 3-byte .part accepts 'bytes 3-5/6', never 'bytes 0-2/6'.
    On failure the partial bytes remain available for the caller's next retry.
    """
    encoding = response.headers.get('Content-Encoding', 'identity').lower()
    if encoding not in ('', 'identity'):
        raise RuntimeError(f'Cannot safely resume Content-Encoding: {encoding}')

    expected_size = None
    if response.status_code == 206:
        content_range = response.headers.get('Content-Range', '')
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
        if match is None:
            raise RuntimeError(f'Invalid Content-Range: {content_range!r}')
        start, end, total = map(int, match.groups())
        if start != resume_byte or not start <= end < total:
            raise RuntimeError(f'Unexpected Content-Range for offset {resume_byte}: {content_range}')
        expected_size = total
    elif response.status_code == 200:
        length = response.headers.get('Content-Length')
        if length is not None:
            expected_size = int(length)
    else:
        raise RuntimeError(f'Expected HTTP 200 or 206, got {response.status_code}')

    append = response.status_code == 206 and resume_byte > 0
    written_size = resume_byte if append else 0
    with open(part_path, 'ab' if append else 'wb') as file:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                file.write(chunk)
                written_size += len(chunk)
    if expected_size is not None and os.path.getsize(part_path) != expected_size:
        raise RuntimeError(f'Incomplete transfer: expected {expected_size} bytes in {part_path}')
    return written_size


def is_complete_range(response: requests.Response, resume_byte: int) -> bool:
    """A 416 may mean all bytes arrived before an interrupted final rename."""
    if response.status_code != 416 or resume_byte <= 0:
        return False
    match = re.fullmatch(r'bytes \*/(\d+)', response.headers.get('Content-Range', ''))
    return match is not None and int(match.group(1)) == resume_byte
