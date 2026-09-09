#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: config.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: class_path-based YAML loading and recursive object instantiation via jsonargparse.

import copy
from dataclasses import dataclass, field
from typing import Any

import yaml
from jsonargparse import ArgumentParser

from geoacquire.postprocess.base import BasePostprocessor
from geoacquire.regions.base import BaseRegionProvider

from .models import AcquireOptions
from .source import BaseSource


@dataclass(frozen=True)
class ReportingConfig:
    """Optional durable outputs for the three-level run reporting model."""

    json_path: str | None = None
    log_path: str | None = None
    overwrite: bool = False

    def __post_init__(self) -> None:
        if self.json_path is not None and (
            not isinstance(self.json_path, str) or not self.json_path
        ):
            raise ValueError('reporting.json_path must be null or a non-empty path')
        if self.log_path is not None and (
            not isinstance(self.log_path, str) or not self.log_path
        ):
            raise ValueError('reporting.log_path must be null or a non-empty path')


@dataclass
class PipelineConfig:
    """One Source acquisition followed by zero or more target post-processors."""

    source: BaseSource
    acquire: AcquireOptions
    postprocess: list[BasePostprocessor] = field(default_factory=list)
    enable: bool = True
    name: str | None = None
    # Independent pool: one task runs a complete postprocessor chain for a Region.
    postprocess_workers: int = 1

    def __post_init__(self) -> None:
        if self.postprocess_workers < 1:
            raise ValueError('postprocess_workers must be >= 1')
        available = self.source.output_specs
        for processor in self.postprocess:
            available = processor.validate_asset_flow(available)


def _merge_named_pipelines(base: list[Any], override: list[Any]) -> list[Any]:
    """Merge pipeline entries by their stable name while retaining declaration order."""
    merged = copy.deepcopy(base)
    positions: dict[str, int] = {}
    for index, item in enumerate(merged):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']:
            raise ValueError('Every configured pipeline must be a mapping with a non-empty name')
        if item['name'] in positions:
            raise ValueError(f'Duplicate pipeline name: {item["name"]}')
        positions[item['name']] = index

    seen: set[str] = set()
    for item in override:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']:
            raise ValueError('Every pipeline override must be a mapping with a non-empty name')
        name = item['name']
        if name in seen:
            raise ValueError(f'Duplicate pipeline override: {name}')
        seen.add(name)
        if name in positions:
            merged[positions[name]] = deep_merge(merged[positions[name]], item)
        else:
            positions[name] = len(merged)
            merged.append(copy.deepcopy(item))
    return merged


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge mappings, named pipelines, and postprocessors by list position.

    The same class_path inherits omitted init_args. Changing class_path replaces
    that component so constructor arguments from the old class cannot leak in.
    For example GoogleSource -> CustomSource starts with CustomSource's args.
    """
    if 'class_path' in override and override['class_path'] != base.get('class_path'):
        return copy.deepcopy(override)
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key == 'pipelines' and isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = _merge_named_pipelines(merged[key], value)
        elif key == 'postprocess' and isinstance(value, list) and isinstance(merged.get(key), list):
            # Postprocessors have no stable names. A short override updates the
            # corresponding step; [] explicitly disables every default step.
            if not value:
                merged[key] = []
            else:
                items = copy.deepcopy(merged[key])
                for index, item in enumerate(value):
                    if index < len(items) and isinstance(item, dict) and isinstance(items[index], dict):
                        items[index] = deep_merge(items[index], item)
                    elif index < len(items):
                        items[index] = copy.deepcopy(item)
                    else:
                        items.append(copy.deepcopy(item))
                merged[key] = items
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _list_item(items: list[Any], segment: str, path: str) -> Any:
    """Resolve either a numeric list index or a mapping item with a matching name."""
    if segment.isdigit():
        index = int(segment)
        if index >= len(items):
            raise ValueError(f'Override path index is out of range: {path}')
        return items[index]
    matches = [item for item in items if isinstance(item, dict) and item.get('name') == segment]
    if not matches:
        raise ValueError(f'No named list item {segment!r} in override path: {path}')
    if len(matches) > 1:
        raise ValueError(f'Ambiguous named list item {segment!r} in override path: {path}')
    return matches[0]


def apply_overrides(config: dict[str, Any], overrides: list[str] | None = None) -> dict[str, Any]:
    """Apply PATH=YAML_VALUE overrides; named list items are addressed by their name."""
    result = copy.deepcopy(config)
    # Each override string from the CLI looks like "pipelines.google.enable=true".
    for expression in overrides or []:
        path, separator, raw_value = expression.partition('=')
        if not separator or not path:
            raise ValueError(f'Override must use PATH=VALUE syntax, got: {expression!r}')
        # Break the path into segments, e.g. ["pipelines", "google", "enable"].
        segments = path.split('.')
        if any(not segment for segment in segments):
            raise ValueError(f'Override path contains an empty segment: {path!r}')

        current: Any = result
        for segment in segments[:-1]:
            if isinstance(current, dict):
                if segment not in current:
                    raise ValueError(f'Unknown override path: {path}')
                current = current[segment]
            elif isinstance(current, list):
                current = _list_item(current, segment, path)
            else:
                raise ValueError(f'Override path traverses a scalar value: {path}')

        leaf = segments[-1]
        if isinstance(current, dict):
            if leaf not in current:
                raise ValueError(f'Unknown override path: {path}')
            current[leaf] = yaml.safe_load(raw_value)
        elif isinstance(current, list) and leaf.isdigit():
            index = int(leaf)
            if index >= len(current):
                raise ValueError(f'Override path index is out of range: {path}')
            current[index] = yaml.safe_load(raw_value)
        else:
            raise ValueError(f'Override path does not identify a configurable value: {path}')
    return result


def load_raw_config(paths: list[str]) -> dict[str, Any]:
    """Read YAML from left to right, merging overrides without instantiating classes."""
    config: dict[str, Any] = {}
    for path in paths:
        with open(path, 'r', encoding='utf-8') as file:
            config = deep_merge(config, yaml.safe_load(file) or {})
    return config


def build_parser() -> ArgumentParser:
    """Declare typed component boundaries; jsonargparse checks constructor arguments."""
    parser = ArgumentParser(exit_on_error=False)
    parser.add_argument('--region', type=BaseRegionProvider, required=True)
    parser.add_argument('--pipelines', type=list[PipelineConfig], required=True)
    parser.add_argument('--reporting', type=ReportingConfig, default=ReportingConfig())
    return parser


def load_config(paths: list[str], overrides: list[str] | None = None) -> Any:
    """
    Load, validate, and instantiate every class_path component.

    Two-step process powered by jsonargparse:
      1. parse_object()  -- validates the dict structure, resolves every
         class_path string to an actual importable class, and checks that
         init_args match the constructor signature.  The result is a parsed
         tree that still contains class references, not live objects.
      2. instantiate()   -- recursively walks the parsed tree and calls
         each class constructor with its init_args, producing fully
         instantiated Python objects.

    Official docs: https://jsonargparse.readthedocs.io/
    GitHub:        https://github.com/mauvilsa/jsonargparse
    """
    parser = build_parser()
    parsed = parser.parse_object(apply_overrides(load_raw_config(paths), overrides))
    return parser.instantiate(parsed)
