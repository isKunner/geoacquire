#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: lidar_catalog.py
# @Time    : 2026/8/30
# @Author  : Kevin
# @Describe: Declarative USGS LiDAR project-name rules loaded from YAML.

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .lidar_tiles import ProjectedGrid


MATCH_FIELDS = ('workunit', 'project', 'lpc_link')
PARSERS = (
    'mgrs',
    'mgrs_tokens',
    'mgrs_zero_x_next_100km',
    'mgrs_absolute_tokens',
    'full_xy',
    'axis_pair',
    'axis_named_pair',
    'number_pair',
    'odd_km_pair',
    'ct_quadrant_2500ft',
    'fl_fdem_5000ft',
    'il_mchenry_2500ft',
    'az_eastern_pima_5k',
    'ca_san_diego_5k',
    'ca_eldorado_2500ft',
    'ca_solano_2640ft',
    'ca_upper_pit_5k',
    'id_southern_15',
    'mgrs_zone_11',
    'zone_tokens',
    'mn_utm_pair',
    'nj_5000ft',
    'mi_2500ft',
    'mi_2500ft_wrapped',
    'nc_panel_5000ft',
    'nc_panel_2500ft',
    'nc_panel_1250ft',
    'nh_2500ft',
    'mt_1000m_wrapped',
    'mgrs_zone14_legacy_columns',
    'mgrs_separated_xy',
    'tn_7000x4000ft',
    'vt_1400m_legacy_center',
    'zone_full_xy',
    'number_pair_date',
    'pa_luzerne_2500ft',
    'pa_dauphin_5000ft',
    'tx_lower_rio_500m',
    'letter_row_number_column',
    'sc_5000ft_quadrants',
    'sc_savannah_5000ft',
    'sc_savannah_2500ft',
    'axis_suffix_pair',
    'wi_sewrpc_10kft',
    'wi_2500ft_quadrants',
    'wa_thurston_4500ft',
)


@dataclass(frozen=True)
class LidarTileScheme:
    """One deterministic filename-to-footprint mapping."""

    parser: str
    grid: ProjectedGrid
    crs: str = 'workunit'
    naming: str | None = None


@dataclass(frozen=True)
class LidarProjectRule:
    """Project selector and the filename schemes used by that project."""

    name: str
    match: dict[str, str]
    schemes: tuple[LidarTileScheme, ...]
    _patterns: tuple[tuple[str, re.Pattern], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Validate/compile once per catalogue load, not on each workunit lookup.
        # Large catalogues otherwise churn Python's bounded global regex cache.
        object.__setattr__(self, '_patterns', tuple(
            (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in self.match.items()
        ))

    def matches(self, workunit: dict[str, Any]) -> bool:
        return all(
            pattern.search(str(workunit.get(name) or '')) is not None
            for name, pattern in self._patterns
        )


class LidarProjectCatalog:
    """Load and validate the editable USGS project-rule table."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        with open(self.path, 'r', encoding='utf-8') as file:
            raw = yaml.safe_load(file) or {}
        if raw.get('version') != 1:
            raise ValueError(f'Unsupported LiDAR project catalogue version: {raw.get("version")!r}')
        projects = raw.get('projects')
        if not isinstance(projects, list):
            raise ValueError('LiDAR project catalogue requires a projects list')
        self.rules = tuple(self._parse_rule(item) for item in projects)

    def find(self, workunit: dict[str, Any]) -> LidarProjectRule | None:
        matches = [rule for rule in self.rules if rule.matches(workunit)]
        if len(matches) > 1:
            names = ', '.join(rule.name for rule in matches)
            raise ValueError(
                f'Multiple LiDAR project rules matched workunit '
                f'{workunit.get("workunit", "unknown")}: {names}'
            )
        return matches[0] if matches else None

    @staticmethod
    def _parse_rule(raw: Any) -> LidarProjectRule:
        if not isinstance(raw, dict):
            raise ValueError('Each LiDAR project rule must be a mapping')
        name = raw.get('name')
        match = raw.get('match')
        schemes = raw.get('schemes')
        if not isinstance(name, str) or not name:
            raise ValueError('Each LiDAR project rule requires a non-empty name')
        if not isinstance(match, dict) or not match:
            raise ValueError(f'LiDAR project rule {name!r} requires a non-empty match mapping')
        unknown = sorted(set(match) - set(MATCH_FIELDS))
        if unknown:
            raise ValueError(f'Unknown match fields in LiDAR project rule {name!r}: {unknown}')
        if not all(isinstance(value, str) and value for value in match.values()):
            raise ValueError(f'Every match expression in LiDAR project rule {name!r} must be a string')
        if not isinstance(schemes, list) or not schemes:
            raise ValueError(f'LiDAR project rule {name!r} requires a non-empty schemes list')
        return LidarProjectRule(
            name=name,
            match=dict(match),
            schemes=tuple(LidarProjectCatalog._parse_scheme(name, item) for item in schemes),
        )

    @staticmethod
    def _parse_scheme(rule_name: str, raw: Any) -> LidarTileScheme:
        if not isinstance(raw, dict):
            raise ValueError(f'Every scheme in LiDAR project rule {rule_name!r} must be a mapping')
        parser = raw.get('parser')
        if parser not in PARSERS and not (
            isinstance(parser, str)
            and (
                re.fullmatch(r'single_number_split_[3-7]', parser) is not None
                or re.fullmatch(r'mgrs_zero_x_columns_[a-hj-np-z]+', parser) is not None
                or re.fullmatch(r'zone_tokens_grid_[a-z]{2}', parser) is not None
                or re.fullmatch(r'mgrs_fixed_\d{1,2}[c-x]', parser) is not None
            )
        ):
            raise ValueError(f'Unsupported parser {parser!r} in LiDAR project rule {rule_name!r}')
        grid = raw.get('grid')
        if not isinstance(grid, dict):
            raise ValueError(f'Scheme {parser!r} in LiDAR project rule {rule_name!r} requires grid')
        required = ('x_scale', 'y_scale', 'x_offset', 'y_offset', 'x_anchor', 'y_anchor', 'width', 'height')
        missing = [key for key in required if key not in grid]
        if missing:
            raise ValueError(f'Scheme {parser!r} in LiDAR project rule {rule_name!r} misses {missing}')
        anchors = (grid['x_anchor'], grid['y_anchor'])
        if any(anchor not in ('min', 'max', 'center') for anchor in anchors):
            raise ValueError(f'Invalid grid anchor in LiDAR project rule {rule_name!r}: {anchors}')
        model = ProjectedGrid(
            x_scale=float(grid['x_scale']),
            y_scale=float(grid['y_scale']),
            x_offset=float(grid['x_offset']),
            y_offset=float(grid['y_offset']),
            x_anchor=str(grid['x_anchor']),
            y_anchor=str(grid['y_anchor']),
            width=float(grid['width']),
            height=float(grid['height']),
            x_padding=float(grid.get('x_padding', 0.0)),
            y_padding=float(grid.get('y_padding', 0.0)),
            x_ceil_step=float(grid.get('x_ceil_step', 0.0)),
            y_ceil_step=float(grid.get('y_ceil_step', 0.0)),
            x_floor_step=float(grid.get('x_floor_step', 0.0)),
            y_floor_step=float(grid.get('y_floor_step', 0.0)),
            x_round_step=float(grid.get('x_round_step', 0.0)),
            y_round_step=float(grid.get('y_round_step', 0.0)),
            x_grid_origin=float(grid.get('x_grid_origin', 0.0)),
            y_grid_origin=float(grid.get('y_grid_origin', 0.0)),
        )
        if model.width <= 0 or model.height <= 0:
            raise ValueError(f'Grid size must be positive in LiDAR project rule {rule_name!r}')
        steps = (
            model.x_ceil_step,
            model.y_ceil_step,
            model.x_floor_step,
            model.y_floor_step,
            model.x_round_step,
            model.y_round_step,
        )
        if any(step < 0 for step in steps):
            raise ValueError(f'Grid rounding step cannot be negative in LiDAR project rule {rule_name!r}')
        if any(sum(step > 0 for step in axis) > 1 for axis in (
            (model.x_ceil_step, model.x_floor_step, model.x_round_step),
            (model.y_ceil_step, model.y_floor_step, model.y_round_step),
        )):
            raise ValueError(f'Choose only one grid rounding direction per axis in {rule_name!r}')
        return LidarTileScheme(
            parser=parser,
            grid=model,
            crs=str(raw.get('crs', 'workunit')),
            naming=str(raw.get('naming')) if raw.get('naming') is not None else None,
        )
