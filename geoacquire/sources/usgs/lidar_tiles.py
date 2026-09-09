#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: lidar_tiles.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Deterministic filename parsers used by the USGS LiDAR project catalogue.

import math
import re
from dataclasses import dataclass

from geoacquire.core.geo import transform_bounds

COL_SETS = ('ABCDEFGH', 'JKLMNPQR', 'STUVWXYZ')
ROW_LETTERS = 'ABCDEFGHJKLMNPQRSTUV'
BAND_LETTERS = 'CDEFGHJKLMNPQRSTUVWX'
BAND_MIN_NORTHINGS = (
    1_100_000,
    2_000_000,
    2_800_000,
    3_700_000,
    4_600_000,
    5_500_000,
    6_400_000,
    7_300_000,
    8_200_000,
    9_100_000,
    0,
    800_000,
    1_700_000,
    2_600_000,
    3_500_000,
    4_400_000,
    5_300_000,
    6_200_000,
    7_000_000,
    7_900_000,
)

MGRS_TILE_RE = re.compile(
    r'(\d{1,2})([C-HJ-NP-X])[_-]?([A-HJ-NP-Z]{2})[_-]?(\d{1,10})(?:[_-](\d{1,5}))?',
    re.IGNORECASE,
)
FULL_XY_TILE_RE = re.compile(r'_(\d{5,6})(\d{7})$', re.IGNORECASE)
AXIS_PAIR_TILE_RE = re.compile(
    r'[_-]([A-Z])(\d{3,7})[_-]?([A-Z])(\d{3,7})(?:_(?:LAS_)?20\d{2})?$',
    re.IGNORECASE,
)
NUMBER_PAIR_TILE_RE = re.compile(
    r'_(\d{4,8})_(\d{4,8})(?:_[A-Z][A-Z0-9_-]*)?$',
    re.IGNORECASE,
)
CT_QUADRANT_TILE_RE = re.compile(r'_(\d{3})(\d{3})_(nw|ne|sw|se)$', re.IGNORECASE)
ZONE_TOKEN_RE = re.compile(
    r'(\d{1,2})[A-Z][_-]?([A-Z]{2})[_-]?(\d{2,10})',
    re.IGNORECASE,
)
LAST_NUMBER_RE = re.compile(
    r'(\d{6,12})(?:_(?:(?:LAS_)?20\d{2}|\d{2}))*[_-]*$',
    re.IGNORECASE,
)
GRID_TOKEN_RE = re.compile(r'[_-]([A-Z]{2})[_-]?(\d{2,10})$', re.IGNORECASE)
ODD_KM_PAIR_RE = re.compile(r'_(\d{4})_?(\d{4})(?:_LAS_20\d{2})?$', re.IGNORECASE)
FL_INDEX_RE = re.compile(r'_(\d{6})(?:_(E|W|N|0901|0902|0903))?$', re.IGNORECASE)
SOLANO_TILE_RE = re.compile(r'_(\d{5})E(\d{5})N$', re.IGNORECASE)
MN_UTM_PAIR_RE = re.compile(r'_(\d{3,4})_(\d{4,5})$', re.IGNORECASE)
NJ_GRID_RE = re.compile(r'_([A-L])(\d{1,2})([A-D])(\d{1,2})$', re.IGNORECASE)
NC_PANEL_RE = re.compile(
    r'_(?:LA_37_)?(\d{8})(?:_+(?:(?:LAS_)?20\d{2}(?:\d{4})?(?:R\d+)?))*_*$',
    re.IGNORECASE,
)
MGRS_SEPARATED_RE = re.compile(
    r'_(\d{1,2})[_-]?([C-HJ-NP-X])[_-]([A-HJ-NP-Z]{2})[_-](\d{5})[_-](\d{5})$',
    re.IGNORECASE,
)
LAS_EXTENSION_RE = re.compile(r'(?:\.la[sz])+$', re.IGNORECASE)
TN_QUADRANT_RE = re.compile(r'_(\d{4})(\d{3})(NE|NW|SE|SW)(?:_LAS_20\d{2})?$', re.IGNORECASE)
ZONE_FULL_XY_RE = re.compile(r'_\d{1,2}[A-Z]{3}(\d{9})(\d{9})$', re.IGNORECASE)
DATED_PAIR_RE = re.compile(r'_(\d{6})_(\d{6})_(20\d{6})$')
PA_LUZERNE_RE = re.compile(r'_(\d{8,9})$')
PA_DAUPHIN_RE = re.compile(r'_(\d{4})(\d{4})PAS$', re.IGNORECASE)
LETTER_ROW_RE = re.compile(r'_([A-Z]{1,2})(\d{1,3})(?:_LAS_20\d{2})?$', re.IGNORECASE)
SC_QUADRANT_RE = re.compile(r'_(\d)(\d)(\d)(\d)[_-]0([1-4])$')
SC_SAVANNAH_RE = re.compile(r'_(\d{8})(?:_\d{2})?$')


@dataclass(frozen=True)
class MGRSTile:
    """Decoded MGRS coordinate plus the original abbreviated numeric tokens.

    easting/northing use UTM metres. easting_token/northing_token retain the
    name's unscaled numbers for reviewed projects that only resemble MGRS.
    """

    zone: int
    band: str
    grid: str
    easting_token: float
    northing_token: float
    easting: float
    northing: float
    resolution: float
    url: str

    @property
    def epsg(self) -> int:
        return (32600 if self.band >= 'N' else 32700) + self.zone


@dataclass(frozen=True)
class ProjectedTile:
    """Parser output, not a footprint: x/y may still need the YAML grid scale.

    For axis_pair, E123_N456 yields x=123, y=456; ProjectedGrid turns those
    tokens into a bounding box in the scheme's CRS. url is provenance only.
    """

    x: float
    y: float
    naming: str
    url: str


@dataclass(frozen=True)
class ProjectedGrid:
    """Apply scale/offset, optional grid rounding, anchor and padding to tokens.

    With scale=(1000,1000), offset=(0,0), anchors=(min,min), size=(1000,1000),
    token (123,456) becomes bounds (123000,456000,124000,457000).
    All resulting distances use the scheme CRS units, not necessarily metres.
    """

    x_scale: float
    y_scale: float
    x_offset: float
    y_offset: float
    x_anchor: str
    y_anchor: str
    width: float
    height: float
    x_padding: float = 0.0
    y_padding: float = 0.0
    x_ceil_step: float = 0.0
    y_ceil_step: float = 0.0
    x_floor_step: float = 0.0
    y_floor_step: float = 0.0
    x_round_step: float = 0.0
    y_round_step: float = 0.0
    x_grid_origin: float = 0.0
    y_grid_origin: float = 0.0

    def bounds(self, tile: ProjectedTile) -> tuple[float, float, float, float]:
        # Wisconsin county grids round truncated names onto shifted grid lines.
        # The origin belongs to the grid, not to the encoded coordinate: with
        # origin=530 and step=4500, 738000 restores to 738530, not 738000.
        x = tile.x * self.x_scale + self.x_offset - self.x_grid_origin
        y = tile.y * self.y_scale + self.y_offset - self.y_grid_origin
        # Some 1.5 km USGS grids store only the containing kilometre in the
        # filename. Their physical lower-left corner is the next 1.5 km grid
        # line, so a fixed offset cannot describe every tile.
        x = self._ceil_to_step(x, self.x_ceil_step)
        y = self._ceil_to_step(y, self.y_ceil_step)
        x = self._floor_to_step(x, self.x_floor_step)
        y = self._floor_to_step(y, self.y_floor_step)
        # Other deliveries round coordinates to the nearest kilometre. A
        # 122000 token then represents 121500 on a 1.5 km grid, not 123000.
        if self.x_round_step > 0:
            x = round(x / self.x_round_step) * self.x_round_step
        if self.y_round_step > 0:
            y = round(y / self.y_round_step) * self.y_round_step
        x += self.x_grid_origin
        y += self.y_grid_origin
        minx, maxx = self._axis_bounds(x, self.width, self.x_anchor)
        miny, maxy = self._axis_bounds(y, self.height, self.y_anchor)
        return (
            minx - self.x_padding,
            miny - self.y_padding,
            maxx + self.x_padding,
            maxy + self.y_padding,
        )

    @staticmethod
    def _ceil_to_step(coordinate: float, step: float) -> float:
        if step <= 0:
            return coordinate
        ratio = coordinate / step
        nearest = round(ratio)
        if math.isclose(ratio, nearest, rel_tol=0.0, abs_tol=1e-9):
            return nearest * step
        return math.ceil(ratio) * step

    @staticmethod
    def _floor_to_step(coordinate: float, step: float) -> float:
        if step <= 0:
            return coordinate
        ratio = coordinate / step
        nearest = round(ratio)
        if math.isclose(ratio, nearest, rel_tol=0.0, abs_tol=1e-9):
            return nearest * step
        return math.floor(ratio) * step

    @staticmethod
    def _axis_bounds(coordinate: float, size: float, anchor: str) -> tuple[float, float]:
        if anchor == 'min':
            return coordinate, coordinate + size
        if anchor == 'max':
            return coordinate - size, coordinate
        return coordinate - size / 2.0, coordinate + size / 2.0


def parse_mgrs_tile(filename: str, url: str) -> MGRSTile | None:
    """Parse compact and underscore-separated MGRS tile suffixes."""
    stem = LAS_EXTENSION_RE.sub('', filename)
    matches = list(MGRS_TILE_RE.finditer(stem))
    for match in reversed(matches):
        zone = int(match.group(1))
        band = match.group(2).upper()
        grid = match.group(3).upper()
        first_digits = match.group(4)
        second_digits = match.group(5)
        if not 1 <= zone <= 60:
            continue
        # Two separately written numbers are often projected grid coordinates
        # even when a zone/band/grid prefix is also present. Only compact MGRS
        # coordinates are decoded here; the project catalogue selects another
        # explicit parser for separated coordinate pairs.
        if second_digits is not None:
            continue
        if len(first_digits) % 2 or not 2 <= len(first_digits) <= 10:
            continue
        midpoint = len(first_digits) // 2
        easting_digits = first_digits[:midpoint]
        northing_digits = first_digits[midpoint:]

        precision = len(easting_digits)
        resolution = float(10 ** (5 - precision))
        try:
            column = COL_SETS[zone % 3 - 1].index(grid[0]) + 1
            row = ROW_LETTERS.index(grid[1])
            band_index = BAND_LETTERS.index(band)
        except ValueError:
            continue

        easting = column * 100_000 + int(easting_digits) * resolution
        row_offset = 0 if zone % 2 else 5
        northing = ((row - row_offset) % 20) * 100_000
        minimum_northing = BAND_MIN_NORTHINGS[band_index]
        while northing < minimum_northing:
            northing += 2_000_000
        northing += int(northing_digits) * resolution
        return MGRSTile(
            zone=zone,
            band=band,
            grid=grid,
            easting_token=float(easting_digits),
            northing_token=float(northing_digits),
            easting=easting,
            northing=northing,
            resolution=resolution,
            url=url,
        )
    return None


def _parse_full_xy(filename: str, url: str) -> ProjectedTile | None:
    """Parse a filename ending in one concatenated easting/northing pair."""
    stem = LAS_EXTENSION_RE.sub('', filename)
    match = FULL_XY_TILE_RE.search(stem)
    if not match:
        return None
    return ProjectedTile(float(match.group(1)), float(match.group(2)), 'full_xy', url)


def _parse_axis_pair(filename: str, url: str) -> ProjectedTile | None:
    """Parse axis-labelled coordinates such as ``E1234_N5678``."""
    stem = LAS_EXTENSION_RE.sub('', filename)
    match = AXIS_PAIR_TILE_RE.search(stem)
    if not match:
        return None
    naming = f'axis_pair_{match.group(1).lower()}{match.group(3).lower()}'
    return ProjectedTile(float(match.group(2)), float(match.group(4)), naming, url)


def _parse_axis_named_pair(filename: str, url: str) -> ProjectedTile | None:
    """Map E/W-labelled values to X and N/S-labelled values to Y."""
    stem = LAS_EXTENSION_RE.sub('', filename)
    match = AXIS_PAIR_TILE_RE.search(stem)
    if not match:
        return None
    values = {
        match.group(1).upper(): float(match.group(2)),
        match.group(3).upper(): float(match.group(4)),
    }
    x = values.get('E', values.get('W'))
    y = values.get('N', values.get('S'))
    if x is None or y is None:
        return None
    return ProjectedTile(x, y, 'axis_named_pair', url)


def _parse_fixed_mgrs(filename: str, url: str, parser: str) -> ProjectedTile | None:
    """Decode a grid-square suffix when one project omits its fixed zone/band."""
    prefix = parser.removeprefix('mgrs_fixed_').upper()
    stem = LAS_EXTENSION_RE.sub('', filename)
    match = GRID_TOKEN_RE.search(stem)
    if not match:
        return None
    tile = parse_mgrs_tile(f'{prefix}{match.group(1)}{match.group(2)}.laz', url)
    if tile is None:
        return None
    return ProjectedTile(
        tile.easting,
        tile.northing,
        parser,
        url,
    )


def _parse_number_pair(filename: str, url: str) -> ProjectedTile | None:
    """Parse two underscore-separated numeric coordinate tokens."""
    stem = LAS_EXTENSION_RE.sub('', filename)
    match = NUMBER_PAIR_TILE_RE.search(stem)
    if not match:
        return None
    return ProjectedTile(float(match.group(1)), float(match.group(2)), 'number_pair', url)


def _parse_odd_km_pair(filename: str, url: str) -> ProjectedTile | None:
    """Decode the four-digit odd-kilounit grid used by Illinois 2 kft tiles.

    ``8970`` represents 897,000 ft; ``1001`` represents 1,001,000 ft.
    The odd-kilounit convention makes the otherwise ambiguous zero unambiguous.
    The YAML rule decides whether this coordinate is a corner or a centre.
    """
    match = ODD_KM_PAIR_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match:
        return None
    values = [int(part) for part in match.groups()]
    decoded = [value // 10 if value % 10 == 0 else value for value in values]
    if any(value % 2 != 1 for value in decoded):
        return None
    return ProjectedTile(decoded[0] * 1000.0, decoded[1] * 1000.0, 'odd_km_pair', url)


def _parse_ct_quadrant(filename: str, url: str) -> ProjectedTile | None:
    """Parse Connecticut 2.5 kft quadrants inside a 5 kft parent grid."""
    stem = LAS_EXTENSION_RE.sub('', filename)
    match = CT_QUADRANT_TILE_RE.search(stem)
    if not match:
        return None
    quadrant = match.group(3).lower()
    x = float(match.group(1)) * 1000
    # The three-digit state-plane easting wraps at one million feet: 005
    # means 1,005,000 ft, while 800 remains 800,000 ft.
    if x < 500_000:
        x += 1_000_000
    x += 2500 if 'e' in quadrant else 0
    y = float(match.group(2)) * 1000 + (2500 if 'n' in quadrant else 0)
    return ProjectedTile(x, y, 'ct_quadrant_2500ft', url)


def _parse_fl_index(filename: str, url: str) -> ProjectedTile | None:
    """Decode reviewed Florida FDEM row-major 5,000 ft index numbers.

    East/West use 300 columns; North uses 540. Modern identifiers add
    200000/400000/600000 to the old one-based cell number. This is an index
    grid, not a concatenated easting/northing; the catalogue supplies its CRS.
    """
    match = FL_INDEX_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match:
        return None
    number = int(match.group(1))
    suffix = (match.group(2) or '').upper()
    suffix = {'0901': 'E', '0902': 'W', '0903': 'N'}.get(suffix, suffix)
    definitions = (
        ('E', 200000, 150000, 300, -330000, 2500000),
        ('W', 400000, 150000, 300, 0, 2500000),
        ('N', 600000, 108000, 540, 850000, 970000),
    )
    for zone, offset, count, columns, left, top in definitions:
        if suffix and suffix != zone:
            continue
        index = number - offset if number > offset else number if suffix else 0
        if not 1 <= index <= count:
            continue
        row, column = divmod(index - 1, columns)
        return ProjectedTile(left + column * 5000, top - (row + 1) * 5000, 'fl_fdem_5000ft', url)
    return None


def _parse_zone_tokens(
    filename: str,
    url: str,
    expected_grid: str | None = None,
) -> ProjectedTile | None:
    """Read numeric X/Y tokens after a zone-like prefix without decoding its letters."""
    stem = LAS_EXTENSION_RE.sub('', filename)
    for match in reversed(list(ZONE_TOKEN_RE.finditer(stem))):
        if expected_grid is not None and match.group(2).lower() != expected_grid:
            continue
        digits = match.group(3)
        if len(digits) % 2 or not 2 <= len(digits) <= 10:
            continue
        midpoint = len(digits) // 2
        return ProjectedTile(
            float(digits[:midpoint]),
            float(digits[midpoint:]),
            'zone_tokens',
            url,
        )
    return None


def _parse_split_number(filename: str, url: str, parser: str) -> ProjectedTile | None:
    """Split the final numeric token at the position declared by the project rule."""
    try:
        split = int(parser.removeprefix('single_number_split_'))
    except ValueError:
        return None
    stem = LAS_EXTENSION_RE.sub('', filename)
    match = LAST_NUMBER_RE.search(stem)
    if not match:
        return None
    digits = match.group(1)
    left, right = digits[:split], digits[split:]
    if not (3 <= len(left) <= 7 and 3 <= len(right) <= 7):
        return None
    return ProjectedTile(float(left), float(right), parser, url)


def _parse_wrapped_coordinates(filename: str, url: str, parser: str) -> ProjectedTile | None:
    """Restore omitted million digits only for reviewed state-plane conventions."""
    split = 4 if parser in ('il_mchenry_2500ft', 'nh_2500ft') else 3
    tile = _parse_split_number(filename, url, f'single_number_split_{split}')
    if tile is None:
        return None
    if parser in ('il_mchenry_2500ft', 'nh_2500ft'):
        x = tile.x * 100
        x += 1_000_000 if x < 500_000 else 0
        y = tile.y * 100 + (2_000_000 if parser == 'il_mchenry_2500ft' else 0)
    elif parser in ('az_eastern_pima_5k', 'mt_1000m_wrapped'):
        x = tile.x * 1000
        x += 1_000_000 if x < 500_000 else 0
        y = tile.y * 1000
    else:  # San Diego stores the last three kilometre digits of state-plane Y.
        x = tile.x * 1000 + 6_000_000
        y = tile.y * 1000 + (2_000_000 if tile.y < 500 else 1_000_000)
    return ProjectedTile(x, y, parser, url)


def _parse_eldorado(filename: str, url: str) -> ProjectedTile | None:
    """Read state-plane kilofeet hidden behind this project's zone-like prefix."""
    tile = _parse_zone_tokens(filename, url)
    if tile is None:
        return None
    x = tile.x * 1000 + (6_000_000 if tile.x >= 500 else 7_000_000)
    y = tile.y * 1000 + (1_000_000 if tile.y >= 500 else 2_000_000)
    return ProjectedTile(x, y, 'ca_eldorado_2500ft', url)


def _parse_solano(filename: str, url: str) -> ProjectedTile | None:
    """Restore truncated hundreds to Solano's offset half-mile grid corners."""
    match = SOLANO_TILE_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match:
        return None
    x = ProjectedGrid._ceil_to_step(int(match.group(1)) * 100 - 400, 2640) + 400
    y = ProjectedGrid._ceil_to_step(int(match.group(2)) * 100 - 2560, 2640) + 2560
    return ProjectedTile(x, y, 'ca_solano_2640ft', url)


def _parse_mn_utm_pair(filename: str, url: str) -> ProjectedTile | None:
    """Restore MN's kilometre (327_5191) or hectometre (5315_48935) tokens.

    Precision is encoded by both token lengths. Tile size remains in YAML:
    hectometre names occur in both 500 m and 1 km deliveries.
    """
    match = MN_UTM_PAIR_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match:
        return None
    x, y = match.groups()
    if (len(x), len(y)) not in ((3, 4), (4, 5)):
        return None
    scale = 1000 if len(x) == 3 else 100
    return ProjectedTile(int(x) * scale, int(y) * scale, 'mn_utm_pair', url)


def _parse_nj_grid(filename: str, url: str) -> ProjectedTile | None:
    """Decode NJ's published 40 kft / quadrant / 4x4 grid into a lower-left.

    Official northwest origin is (190000, 925000) US survey feet. For example
    H7B14 is column H, row 7, NE quadrant, row-major cell 14 inside that quadrant.
    """
    match = NJ_GRID_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match:
        return None
    column, row, quadrant, cell = match.groups()
    row, cell = int(row), int(cell)
    if not 1 <= row <= 23 or not 1 <= cell <= 16:
        return None
    x = 190000 + (ord(column.upper()) - ord('A')) * 40000
    top = 925000 - (row - 1) * 40000
    x += 20000 if quadrant.upper() in 'BD' else 0
    top -= 20000 if quadrant.upper() in 'CD' else 0
    x += ((cell - 1) % 4) * 5000
    y = top - ((cell - 1) // 4 + 1) * 5000
    return ProjectedTile(x, y, 'nj_5000ft', url)


def _parse_mi_grid(filename: str, url: str, wrapped: bool) -> ProjectedTile | None:
    """Restore reviewed MI 2.5 kft corners from rounded/truncated kilofeet.

    Both 812 and 813 can denote 812500 ft; round to the nearest 2500 ft line.
    A workunit crossing a million-foot boundary explicitly opts into wrapping;
    its YAML supplies the remaining state-plane million-foot offset.
    """
    tile = _parse_split_number(filename, url, 'single_number_split_3')
    if tile is None or tile.x >= 1000 or tile.y >= 1000:
        return None
    x = tile.x * 1000 + (1000000 if wrapped and tile.x < 500 else 0)
    return ProjectedTile(round(x / 2500) * 2500, round(tile.y * 1000 / 2500) * 2500,
                         'mi_2500ft', url)


def _parse_nc_panel(filename: str, url: str, size: int) -> ProjectedTile | None:
    """Decode the NC SOS LRM panel code, not a concatenated X/Y pair.

    First six digits interleave easting/northing 10 kft digits: 207704 means
    (2700000, 740000). Last two digits select a NW-first row-major subpanel:
    01-04 for 5 kft, 05-20 for 2.5 kft, and 21-84 for 1.25 kft tiles.
    See the official 2013 NC LiDAR specification, section 1.05.2, pp. 27-29.
    """
    match = NC_PANEL_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match:
        return None
    code = match.group(1)
    columns, first = {5000: (2, 1), 2500: (4, 5), 1250: (8, 21)}[size]
    cell = int(code[6:]) - first
    if not 0 <= cell < columns * columns:
        return None
    x = int(code[:6:2]) * 10000 + (cell % columns) * size
    y = int(code[1:6:2]) * 10000 + (columns - 1 - cell // columns) * size
    return ProjectedTile(x, y, f'nc_panel_{size}ft', url)


def _parse_upper_pit(filename: str, url: str) -> ProjectedTile | None:
    """Recover the state-plane grid corner located by a truncated UTM cell.

    Upper Pit labels a 5 kft state-plane tile by the containing 100 m MGRS
    cell. Transform that small cell, not its southwest point: projection
    rotation can otherwise move a rounded point past the desired grid line.
    Both CRSs use NAD83(2011), so this operation needs no datum-grid download.
    """
    tile = parse_mgrs_tile(filename, url)
    if tile is None or tile.zone != 10 or tile.resolution != 100:
        return None
    bounds = transform_bounds(
        (tile.easting, tile.northing, tile.easting + 100, tile.northing + 100),
        'EPSG:6339', 'EPSG:6416',
    )
    x = ProjectedGrid._ceil_to_step(bounds[0], 5000)
    y = ProjectedGrid._ceil_to_step(bounds[1], 5000)
    if not (x <= bounds[2] < x + 5000 and y <= bounds[3] < y + 5000):
        return None  # No unique grid corner in this filename's locator cell.
    return ProjectedTile(x, y, 'ca_upper_pit_5k', url)


def _parse_tn_quadrant(filename: str, url: str) -> ProjectedTile | None:
    """TN reviewed 14,000 x 8,000 ft parents split into four 7,000 x 4,000 tiles.

    2248661NE -> lower-left (2255503, 665378). The parent's abbreviated
    kilofeet omit fixed remainders (503,378); NE adds (7000,4000).
    Only the explicitly reviewed workunits in YAML use this convention.
    """
    match = TN_QUADRANT_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match:
        return None
    east, north, quadrant = match.groups()
    x = int(east) * 1000 + 503 + (7000 if 'E' in quadrant.upper() else 0)
    y = int(north) * 1000 + 378 + (4000 if 'N' in quadrant.upper() else 0)
    return ProjectedTile(x, y, 'tn_7000x4000ft', url)


def _parse_vt_legacy_center(filename: str, url: str) -> ProjectedTile | None:
    """Old VT names reverse the axis labels and omit Y's 100 km digit.

    N4557E527 denotes centre (455700,252700), not northing/easting. Restore
    the unique centre on the 1,400 m grid within Vermont's 0..300 km Y range.
    Modern Statewide names instead use normal E=X/N=Y and must not opt in.
    """
    match = AXIS_PAIR_TILE_RE.search(LAS_EXTENSION_RE.sub('', filename))
    if not match or match[1].upper() != 'N' or match[3].upper() != 'E':
        return None
    if len(match[2]) != 4 or len(match[4]) != 3:
        return None
    x = int(match[2]) * 100
    ys = [int(match[4]) * 100 + offset for offset in (0, 100000, 200000)]
    ys = [y for y in ys if (y - 700) % 1400 == 0]
    if len(ys) != 1 or (x - 700) % 1400:
        return None
    return ProjectedTile(x, ys[0], 'vt_1400m_legacy_center', url)


def parse_catalog_tile(filename: str, url: str, parser: str) -> tuple[ProjectedTile, int | None] | None:
    """Apply exactly the parser selected by the catalogue rule.

    Direct dispatch matters for link lists with thousands of names: a reviewed
    rule should not run every alternative regular expression and then discard
    the unwanted candidates.

    Return (ProjectedTile, MGRS_EPSG_or_None), or None for an unrecognized
    name. The caller applies the YAML grid and resolves the actual source CRS;
    an MGRS EPSG hint must not override an explicit workunit CRS.
    """
    if parser in ('mgrs', 'mgrs_tokens', 'mgrs_zero_x_next_100km', 'mgrs_absolute_tokens'):
        tile = parse_mgrs_tile(filename, url)
        if tile is None:
            return None
        if parser == 'mgrs':
            projected = ProjectedTile(tile.easting, tile.northing, parser, url)
        elif parser == 'mgrs_zero_x_next_100km':
            easting = tile.easting + (100_000 if tile.easting_token == 0 else 0)
            projected = ProjectedTile(easting, tile.northing, parser, url)
        elif parser == 'mgrs_absolute_tokens':
            # A few vendors retain MGRS zone/grid letters but store absolute
            # projected coordinate digits (rather than within-square digits).
            scale = tile.resolution * 10
            encoded_northing = tile.northing_token * tile.resolution
            million = math.floor((tile.northing - encoded_northing) / 1_000_000) * 1_000_000
            projected = ProjectedTile(
                tile.easting_token * scale,
                million + tile.northing_token * scale,
                parser,
                url,
            )
        else:
            projected = ProjectedTile(tile.easting_token, tile.northing_token, parser, url)
        return projected, tile.epsg

    if parser.startswith('mgrs_zero_x_columns_'):
        # Some deliveries mix a wrong previous-column label at X=000 with
        # valid zero eastings in other columns. Repair only reviewed columns,
        # e.g. columns_t repairs T000 but leaves U000 at the same 300 km line.
        tile = parse_mgrs_tile(filename, url)
        if tile is None:
            return None
        columns = parser.removeprefix('mgrs_zero_x_columns_').upper()
        x = tile.easting + (100000 if tile.easting_token == 0 and tile.grid[0] in columns else 0)
        return ProjectedTile(x, tile.northing, parser, url), tile.epsg

    if parser in ('sc_savannah_5000ft', 'sc_savannah_2500ft'):
        # Savannah/PeeDee interleaves X/Y kilofeet in eight digits. The
        # reviewed delivered grid has a non-zero origin, recorded in YAML.
        match = SC_SAVANNAH_RE.search(LAS_EXTENSION_RE.sub('', filename))
        if not match:
            return None
        digits = match[1]
        x, y = int(digits[::2]) * 1000, int(digits[1::2]) * 1000
        if parser == 'sc_savannah_2500ft':
            x, y = round(x / 2500) * 2500, round(y / 2500) * 2500
        selected = ProjectedTile(x, y, parser, url)
    elif parser == 'axis_suffix_pair':
        # Oneida writes 240E_200N, not E240N200. Return raw kilounits;
        # the catalogue still owns scale, tile size and coordinate system.
        match = re.search(r'_(\d{3,7})E_(\d{3,7})N(?:_LAS_20\d{2})?$',
                          LAS_EXTENSION_RE.sub('', filename), flags=re.I)
        selected = ProjectedTile(int(match[1]), int(match[2]), parser, url) if match else None
    elif parser == 'wi_sewrpc_10kft':
        match = re.search(r'_LD15_(\d{4})_(\d{3})(?:_LAS_20\d{2})?$',
                          LAS_EXTENSION_RE.sub('', filename), flags=re.I)
        selected = ProjectedTile(int(match[1]) * 1000, int(match[2]) * 1000, parser, url) if match else None
    elif parser == 'wi_2500ft_quadrants':
        # Ordinary tiles truncate the 2500-ft corner to kilometres. Named
        # quadrants instead split a 5000-ft parent: 03300130NE -> (332500,132500).
        match = re.search(r'_(\d{4})(\d{4})(NE|NW|SE|SW)?$',
                          LAS_EXTENSION_RE.sub('', filename), flags=re.I)
        if not match:
            return None
        x, y = int(match[1]) * 1000, int(match[2]) * 1000
        quadrant = (match[3] or '').upper()
        if quadrant:
            if x % 5000 or y % 5000:
                return None
            x += 2500 if 'E' in quadrant else 0
            y += 2500 if 'N' in quadrant else 0
        else:
            x, y = math.ceil(x / 2500) * 2500, math.ceil(y / 2500) * 2500
        selected = ProjectedTile(x, y, parser, url)
    elif parser == 'wa_thurston_4500ft':
        # Five X digits change precision below 1,000,000 ft. These are two
        # observed forms in this delivery, not a general interpretation of W.
        match = re.search(r'_w(\d{5})n(\d{5})$', LAS_EXTENSION_RE.sub('', filename), flags=re.I)
        if not match:
            return None
        x = int(match[1])
        selected = ProjectedTile(x * (10 if x >= 90000 else 100), int(match[2]) * 10, parser, url)
    elif parser == 'letter_row_number_column':
        # VA Southwest: A146 is column 146, row A=0; AA=26 (Excel letters).
        # The reviewed YAML supplies the grid origin, spacing and CRS.
        match = LETTER_ROW_RE.search(LAS_EXTENSION_RE.sub('', filename))
        if not match:
            return None
        row = 0
        for letter in match[1].upper():
            row = row * 26 + ord(letter) - ord('A') + 1
        selected = ProjectedTile(int(match[2]), row - 1, parser, url)
    elif parser == 'sc_5000ft_quadrants':
        # Four interleaved parent digits: 3431 -> X=330000,Y=410000.
        # 01=NW,02=NE,03=SW,04=SE; YAML restores the project's million X.
        match = SC_QUADRANT_RE.search(LAS_EXTENSION_RE.sub('', filename))
        if not match:
            return None
        a, b, c, d, quadrant = map(int, match.groups())
        x = (a * 10 + c) * 10000 + (5000 if quadrant in (2, 4) else 0)
        y = (b * 10 + d) * 10000 + (5000 if quadrant in (1, 2) else 0)
        selected = ProjectedTile(x, y, parser, url)
    elif parser == 'tn_7000x4000ft':
        selected = _parse_tn_quadrant(filename, url)
    elif parser == 'vt_1400m_legacy_center':
        selected = _parse_vt_legacy_center(filename, url)
    elif parser == 'zone_full_xy':
        # Portland B24 pads each absolute UTM coordinate to nine digits.
        match = ZONE_FULL_XY_RE.search(LAS_EXTENSION_RE.sub('', filename))
        selected = ProjectedTile(int(match[1]), int(match[2]), parser, url) if match else None
    elif parser == 'number_pair_date':
        # Klamath: the final YYYYMMDD is an acquisition date, never Y.
        match = DATED_PAIR_RE.search(LAS_EXTENSION_RE.sub('', filename))
        selected = ProjectedTile(int(match[1]), int(match[2]), parser, url) if match else None
    elif parser == 'pa_luzerne_2500ft':
        # Eight digits omit X's leading 2 million; nine retain it. Y is top.
        match = PA_LUZERNE_RE.search(LAS_EXTENSION_RE.sub('', filename))
        if not match:
            return None
        digits = match[1]
        x = int(digits[:-4]) * 100 + (2000000 if len(digits) == 8 else 0)
        selected = ProjectedTile(x, int(digits[-4:]) * 100, parser, url)
    elif parser == 'pa_dauphin_5000ft':
        # 40002190PAS stores Y first: upper-left (2190000,400000).
        match = PA_DAUPHIN_RE.search(LAS_EXTENSION_RE.sub('', filename))
        selected = ProjectedTile(int(match[2]) * 1000, int(match[1]) * 100, parser, url) if match else None
    elif parser == 'tx_lower_rio_500m':
        # Reviewed 1 km parent: a=NW, b=NE, c=SE, d=SW. No suffix is not
        # a quadrant; those deliveries select the separate 1 km YAML scheme.
        match = re.search(r'_([a-d])\.la[sz]$', filename, flags=re.I)
        if not match:
            return None
        parsed = parse_catalog_tile(filename, url, 'mgrs_zone14_legacy_columns')
        if parsed is None:
            return None
        tile, epsg = parsed
        quadrant = match[1].lower()
        return ProjectedTile(tile.x + (500 if quadrant in 'bc' else 0),
                             tile.y + (500 if quadrant in 'ab' else 0), parser, url), epsg
    elif parser == 'full_xy':
        selected = _parse_full_xy(filename, url)
    elif parser == 'axis_pair':
        selected = _parse_axis_pair(filename, url)
    elif parser == 'axis_named_pair':
        selected = _parse_axis_named_pair(filename, url)
    elif parser == 'number_pair':
        selected = _parse_number_pair(filename, url)
    elif parser == 'odd_km_pair':
        selected = _parse_odd_km_pair(filename, url)
    elif parser == 'ct_quadrant_2500ft':
        selected = _parse_ct_quadrant(filename, url)
    elif parser == 'fl_fdem_5000ft':
        selected = _parse_fl_index(filename, url)
    elif parser in ('il_mchenry_2500ft', 'az_eastern_pima_5k', 'ca_san_diego_5k',
                    'nh_2500ft', 'mt_1000m_wrapped'):
        selected = _parse_wrapped_coordinates(filename, url, parser)
    elif parser == 'ca_eldorado_2500ft':
        selected = _parse_eldorado(filename, url)
    elif parser == 'ca_solano_2640ft':
        selected = _parse_solano(filename, url)
    elif parser == 'ca_upper_pit_5k':
        selected = _parse_upper_pit(filename, url)
    elif parser == 'mn_utm_pair':
        selected = _parse_mn_utm_pair(filename, url)
    elif parser == 'nj_5000ft':
        selected = _parse_nj_grid(filename, url)
    elif parser in ('mi_2500ft', 'mi_2500ft_wrapped'):
        selected = _parse_mi_grid(filename, url, parser.endswith('_wrapped'))
    elif parser in ('nc_panel_5000ft', 'nc_panel_2500ft', 'nc_panel_1250ft'):
        selected = _parse_nc_panel(filename, url, int(parser.split('_')[-1][:-2]))
    elif parser == 'mgrs_zone14_legacy_columns':
        # Reviewed ND/SD and TX deliveries count column O: JKLMNOPQ,
        # unlike standard MGRS JKLMNPQR. Keep this repair opt-in by workunit.
        corrected = re.sub(r'(14[R-U][_-]?)([OPQ])(?=[A-HJ-NP-V][_-]?\d)',
                           lambda m: m[1] + {'O': 'P', 'P': 'Q', 'Q': 'R'}[m[2].upper()],
                           filename, flags=re.I)
        return parse_catalog_tile(corrected, url, 'mgrs')
    elif parser == 'mgrs_separated_xy':
        # NM SouthEast stores full within-square coordinates separately:
        # 13S_DR_78499_58000 -> 13SDR7849958000. Other separated numeric
        # names may be absolute coordinates, so this is an explicit opt-in.
        stem = LAS_EXTENSION_RE.sub('', filename)
        match = MGRS_SEPARATED_RE.search(stem)
        if match is None:
            return None
        return parse_catalog_tile('_' + ''.join(match.groups()) + '.laz', url, 'mgrs')
    elif parser == 'id_southern_15':
        # Workunit 15 has two reviewed TTM -> QG letter errors and NH eastings
        # ending in 000 that refer to the next 100 km square. No other project
        # opts into these local repairs.
        corrected = re.sub(r'11TTM470(965|980)(?=\.la[sz]$)', r'11TQG470\1', filename, flags=re.I)
        tile = parse_mgrs_tile(corrected, url)
        if tile is None:
            return None
        x = tile.easting + (100000 if tile.grid == 'NH' and tile.easting_token == 0 else 0)
        return ProjectedTile(x, tile.northing, parser, url), tile.epsg
    elif parser == 'mgrs_zone_11':
        # A few Fresno filenames incorrectly say zone 10 before a zone-11
        # square (for example 10SKA). The reviewed rule alone opts into repair.
        stem = LAS_EXTENSION_RE.sub('', filename)
        matches = list(MGRS_TILE_RE.finditer(stem))
        if not matches:
            return None
        match = matches[-1]
        corrected = stem[:match.start(1)] + '11' + stem[match.end(1):]
        return parse_catalog_tile(corrected, url, 'mgrs')
    elif parser == 'zone_tokens':
        selected = _parse_zone_tokens(filename, url)
    elif parser.startswith('zone_tokens_grid_'):
        selected = _parse_zone_tokens(
            filename,
            url,
            parser.removeprefix('zone_tokens_grid_').lower(),
        )
    elif parser.startswith('mgrs_fixed_'):
        selected = _parse_fixed_mgrs(filename, url, parser)
    elif parser.startswith('single_number_split_'):
        selected = _parse_split_number(filename, url, parser)
    else:
        selected = None
    return (selected, None) if selected is not None else None
