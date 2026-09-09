#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: test_lidar_rules.py
# @Time    : 2026/8/31
# @Author  : Kevin
# @Describe: Offline regression from official LAZ headers and naming edge cases.

import json
import os
import unittest
from unittest.mock import Mock, patch

from pyproj import CRS
from shapely.geometry import GeometryCollection

from geoacquire.core.models import Region
from geoacquire.sources.usgs.lidar import USGSLidarSource
from geoacquire.sources.usgs.lidar_catalog import LidarProjectCatalog
from geoacquire.sources.usgs.lidar_tiles import ProjectedGrid, parse_catalog_tile
from scripts.replay_lidar_audit import CachedAuditSource


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, 'configs', 'usgs_lidar_projects.yaml')


class LidarRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Explicit evidence list: adding a fixture requires opting it into this
        # regression, rather than accidentally loading unrelated research JSON.
        cls.groups = []
        for filename in (
            'lidar_headers.json', 'lidar_headers_mn.json',
            'lidar_headers_ov.json', 'lidar_headers_w.json',
        ):
            with open(os.path.join(ROOT, 'tests', 'fixtures', filename), encoding='utf-8') as file:
                cls.groups.extend(json.load(file))

    def setUp(self):
        self.source = USGSLidarSource(project_catalog_path=CATALOG)

    def test_official_header_footprints_and_declared_crs(self):
        """Keep observations, not coordinates computed by the parser under test.

        Bounds are measured from small official LAZ-header responses. The 0.1
        source-unit tolerance allows header/fixture decimal rounding, not a
        geographic padding shortcut. Declared EPSG IDs check zone/units; this
        test does not certify the provider's datum or every WKT parameter.
        """
        for item in self.groups:
            rule = self.source.catalog.find(item)
            self.assertIsNotNone(rule, item['workunit'])
            self.assertEqual(rule.name, item['rule'])
            for filename, observed, declared_crs in item['samples']:
                with self.subTest(workunit=item['workunit'], filename=filename):
                    tile = self.source._parse_rule_tile(filename, item['url_prefix'] + '/' + filename, item, rule, [])
                    self.assertIsNotNone(tile)
                    self.assertLessEqual(tile.bounds[0], observed[0] + 0.1)
                    self.assertLessEqual(tile.bounds[1], observed[1] + 0.1)
                    self.assertGreaterEqual(tile.bounds[2], observed[2] - 0.1)
                    self.assertGreaterEqual(tile.bounds[3], observed[3] - 0.1)
                    if declared_crs:
                        self.assertTrue(CRS(tile.source_crs).equals(CRS(declared_crs), ignore_axis_order=True))

    def test_me_million_boundary(self):
        expected = {
            '19TCK388906': (388000, 4906000),
            '19TDL451082': (451000, 5082000),
        }
        for suffix, coordinates in expected.items():
            tile, _ = parse_catalog_tile('USGS_LPC_ME_' + suffix + '.laz', '', 'mgrs_absolute_tokens')
            self.assertEqual((tile.x, tile.y), coordinates)

    def test_il_odd_kilofeet_and_mchenry_zero(self):
        tile, _ = parse_catalog_tile('USGS_LPC_IL_8970_1001.laz', '', 'odd_km_pair')
        self.assertEqual((tile.x, tile.y), (897000, 1001000))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_IL_8960_1002.laz', '', 'odd_km_pair'))
        tile, _ = parse_catalog_tile('USGS_LPC_IL_LAS_00000000.laz', '', 'il_mchenry_2500ft')
        self.assertEqual((tile.x, tile.y), (1000000, 2000000))

    def test_fl_new_old_indices_and_zone_mismatch(self):
        samples = {
            '479896_W': (475000, 1165000),
            '251165_E': (490000, 1645000),
            '647650_N': (1495000, 525000),
            '050300_E': (665000, 1660000),
            '318450_0901': (915000, 525000),
        }
        for suffix, coordinates in samples.items():
            tile, _ = parse_catalog_tile('USGS_LPC_FL_' + suffix + '.laz', '', 'fl_fdem_5000ft')
            self.assertEqual((tile.x, tile.y), coordinates)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_FL_251165_W.laz', '', 'fl_fdem_5000ft'))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_FL_050300.laz', '', 'fl_fdem_5000ft'))

    def test_nc_numeric_padding_is_not_a_placeholder(self):
        self.assertFalse(self.source._is_project_placeholder('USGS_LPC_NC_LA_37_10380906_.laz'))
        self.assertFalse(self.source._is_project_placeholder('USGS_LPC_NC_LA_37_10380906__.laz'))
        self.assertTrue(self.source._is_project_placeholder('USGS_LPC_AL_Project_B17_.laz'))
        # Exercise the link-list boundary too, not just the standalone regex.
        item, filename = next((g, s[0]) for g in self.groups for s in g['samples']
                              if g['workunit'].startswith('NC_') and s[0].endswith('_.laz'))
        workunit = dict(item, lpc_link=item['url_prefix'].rsplit('/', 1)[0])
        self.source._fetch_text = Mock(return_value=(
            item['url_prefix'] + '/' + filename + '\nhttps://example.invalid/USGS_LPC_Project_.laz'
        ))
        plan = self.source._load_workunit_plan(workunit)
        self.assertEqual((plan.filenames, len(plan.tiles), plan.ignored_placeholders), (1, 1, 1))
        self.assertEqual(plan.tiles[0].filename, filename)

    def test_nc_documented_panels_and_suffixes(self):
        # Official specification pp. 27-29: parent 207704 is (2700000,740000).
        cases = [
            ('20770404', 'nc_panel_5000ft', (2705000, 740000)),
            ('20770416', 'nc_panel_2500ft', (2707500, 742500)),
            ('20770460', 'nc_panel_1250ft', (2708750, 743750)),
        ]
        for code, parser, expected in cases:
            for ending in ('', '_', '__LAS_2019', '_20190331'):
                tile, _ = parse_catalog_tile('USGS_LPC_NC_LA_37_' + code + ending + '.laz', '', parser)
                self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_NC_20770404.laz', '', 'nc_panel_2500ft'))

    def test_nj_documented_grid(self):
        # Published NJ grid: northwest origin (190000,925000), A1 first parent.
        for suffix, expected in [('A1A1', (190000, 920000)), ('L23D16', (665000, 5000)),
                                 ('H7B14', (495000, 665000))]:
            tile, _ = parse_catalog_tile('USGS_LPC_NJ_' + suffix + '.laz', '', 'nj_5000ft')
            self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_NJ_A1A17.laz', '', 'nj_5000ft'))

    def test_mn_precision_and_mi_rounding(self):
        for suffix, expected in [('327_5191', (327000, 5191000)), ('5315_48935', (531500, 4893500))]:
            tile, _ = parse_catalog_tile('USGS_LPC_MN_' + suffix + '.laz', '', 'mn_utm_pair')
            self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_MN_327_51910.laz', '', 'mn_utm_pair'))
        for suffix in ('812792', '813793'):
            tile, _ = parse_catalog_tile('USGS_LPC_MI_' + suffix + '.laz', '', 'mi_2500ft')
            self.assertEqual((tile.x, tile.y), (812500, 792500))
        tile, _ = parse_catalog_tile('USGS_LPC_MI_000355.laz', '', 'mi_2500ft_wrapped')
        self.assertEqual((tile.x, tile.y), (1000000, 355000))

    def test_dated_coordinate_suffixes(self):
        tile, _ = parse_catalog_tile('USGS_LPC_NY_e1370n2352_2019.laz', '', 'axis_pair')
        self.assertEqual((tile.x, tile.y), (1370, 2352))
        tile, _ = parse_catalog_tile('USGS_LPC_NY_t_7220067850_2017_LAS_2019.laz', '', 'single_number_split_5')
        self.assertEqual((tile.x, tile.y), (72200, 67850))

    def test_legacy_mgrs_columns_are_opt_in(self):
        filename = 'USGS_LPC_ND_14TOT100090.laz'
        self.assertIsNone(parse_catalog_tile(filename, '', 'mgrs'))
        tile, _ = parse_catalog_tile(filename, '', 'mgrs_zone14_legacy_columns')
        self.assertEqual((tile.x, tile.y), (610000, 5209000))
        tile, _ = parse_catalog_tile('USGS_LPC_SD_14TPR010000.laz', '', 'mgrs_zone14_legacy_columns')
        self.assertEqual((tile.x, tile.y), (701000, 5000000))

    def test_nm_separated_mgrs_is_opt_in(self):
        filename = 'USGS_LPC_NM_13S_DR_78499_58000.laz'
        self.assertIsNone(parse_catalog_tile(filename, '', 'mgrs'))
        tile, epsg = parse_catalog_tile(filename, '', 'mgrs_separated_xy')
        self.assertEqual((tile.x, tile.y, epsg), (478499, 3558000, 32613))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_NM_13S_DR_784_580.laz', '', 'mgrs_separated_xy'))

    def test_tn_rectangular_quadrants(self):
        for quadrant, expected in [('SW', (2248503, 661378)), ('SE', (2255503, 661378)),
                                   ('NW', (2248503, 665378)), ('NE', (2255503, 665378))]:
            tile, _ = parse_catalog_tile('USGS_LPC_TN_2248661' + quadrant + '_LAS_2018.laz', '', 'tn_7000x4000ft')
            self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_TN_2248661.laz', '', 'tn_7000x4000ft'))

    def test_oh_truncated_kilofeet_need_1250_grid(self):
        tile, _ = parse_catalog_tile('USGS_LPC_OH_BN13270395.laz', '', 'single_number_split_4')
        model = ProjectedGrid(1000, 1000, 0, 0, 'min', 'min', 1250, 1250,
                              x_ceil_step=1250, y_ceil_step=1250)
        self.assertEqual(model.bounds(tile), (1327500, 395000, 1328750, 396250))

    def test_tx_neches_unequal_precision(self):
        # Seven digits are X kilometres + Y hectometres, not ordinary MGRS.
        name = 'USGS_LPC_TX_Neches_B5_2016_15SUR3405520_LAS_2018.laz'
        self.assertIsNone(parse_catalog_tile(name, '', 'mgrs'))
        tile, _ = parse_catalog_tile(name, '', 'single_number_split_3')
        model = ProjectedGrid(1000, 100, 0, 3000000, 'min', 'min', 1500, 1500, x_ceil_step=1500)
        self.assertEqual(model.bounds(tile), (340500, 3552000, 342000, 3553500))

    def test_tx_legacy_columns_and_subtiles(self):
        for suffix, expected in [('a', (642000, 2870500)), ('b', (642500, 2870500)),
                                 ('c', (642500, 2870000)), ('d', (642000, 2870000))]:
            tile, _ = parse_catalog_tile('USGS_LPC_TX_14rop420700_' + suffix + '.laz', '', 'tx_lower_rio_500m')
            self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_TX_14rop420700.laz', '', 'tx_lower_rio_500m'))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_TX_14rop420700_e.laz', '', 'tx_lower_rio_500m'))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_TX_14rop420700.laz', '', 'mgrs'))

    def test_zero_easting_repair_is_column_specific(self):
        for code in ('15STS000390', '15SUS000390'):
            tile, _ = parse_catalog_tile('USGS_LPC_TX_' + code + '_LAS_2019.laz', '', 'mgrs_zero_x_columns_t')
            self.assertEqual((tile.x, tile.y), (300000, 3639000))
        tile, _ = parse_catalog_tile('USGS_LPC_TX_15SVS000390.laz', '', 'mgrs_zero_x_columns_t')
        self.assertEqual(tile.x, 400000)

    def test_sc_savannah_interleaved_coordinates(self):
        for code, parser, expected in [('11508021','sc_savannah_5000ft',(1582000,1001000)),
                                       ('10866978_69','sc_savannah_2500ft',(1867500,697500)),
                                       ('20072003_70','sc_savannah_2500ft',(2020000,702500))]:
            tile, _ = parse_catalog_tile('USGS_LPC_SC_' + code + '.laz', '', parser)
            self.assertEqual((tile.x,tile.y),expected)

    def test_or_padding_and_date_are_not_mgrs_coordinates(self):
        tile, _ = parse_catalog_tile('USGS_LPC_OR_10TER000511500005026500.laz', '', 'zone_full_xy')
        self.assertEqual((tile.x, tile.y), (511500, 5026500))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_OR_10TER5115005026500.laz', '', 'zone_full_xy'))
        tile, _ = parse_catalog_tile('USGS_LPC_OR_FREKLA_421088_942808_20180917.laz', '', 'number_pair_date')
        self.assertEqual((tile.x, tile.y), (421088, 942808))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_OR_421088_942808.laz', '', 'number_pair_date'))

    def test_pa_coordinate_order_and_omitted_million(self):
        for code, expected in [('46754125', (2467500, 412500)), ('255003975', (2550000, 397500))]:
            tile, _ = parse_catalog_tile('USGS_LPC_PA_' + code + '.laz', '', 'pa_luzerne_2500ft')
            self.assertEqual((tile.x, tile.y), expected)
        tile, _ = parse_catalog_tile('USGS_LPC_PA_40002190PAS.laz', '', 'pa_dauphin_5000ft')
        self.assertEqual((tile.x, tile.y), (2190000, 400000))

    def test_vt_old_axis_labels_and_unique_grid_phase(self):
        for code, expected in [('N4557E527', (455700, 252700)), ('N4263E757', (426300, 175700)),
                               ('N4585E399', (458500, 39900))]:
            tile, _ = parse_catalog_tile('USGS_LPC_VT_' + code + '_LAS_2018.laz', '', 'vt_1400m_legacy_center')
            self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_VT_N2513E4445.laz', '', 'vt_1400m_legacy_center'))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_VT_N4558E527.laz', '', 'vt_1400m_legacy_center'))

    def test_va_letter_rows_and_sc_quadrants(self):
        for code, expected in [('A146', (146, 0)), ('Z99', (99, 25)), ('AA100', (100, 26)), ('BC178', (178, 54))]:
            tile, _ = parse_catalog_tile('USGS_LPC_VA_' + code + '_LAS_2018.laz', '', 'letter_row_number_column')
            self.assertEqual((tile.x, tile.y), expected)
        for code, expected in [('3431-01', (330000, 415000)), ('3431_02', (335000, 415000)),
                               ('3431_03', (330000, 410000)), ('3431_04', (335000, 410000))]:
            tile, _ = parse_catalog_tile('USGS_LPC_SC_' + code + '.laz', '', 'sc_5000ft_quadrants')
            self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_SC_3431_05.laz', '', 'sc_5000ft_quadrants'))

    def test_new_rules_do_not_claim_unreviewed_workunits(self):
        for name in ('OH_StatewideP3_99_B21', 'TX_Neches_B99_2016', 'TN_Middle_B99_2018',
                     'VA_FEMA_R3_Southwest_Z_2016', 'VT_Statewide_99_A23',
                     'TX_Pecos_Dallas_B2b_2018', 'TX_West_Central_B11_2018',
                     'UT_StatewideSouth_3_2020', 'VA_West_Chesapeake_B1_2017',
                     'WI_12County_99_B22', 'WA_NorthCentral_99_2021',
                     'WY_GoshenCounty_B1_2017', 'WA_Western_North_2016'):
            self.assertIsNone(self.source.catalog.find({'workunit': name, 'project': '', 'lpc_link': ''}))

    def test_wi_shifted_grid_rounding(self):
        tile, _ = parse_catalog_tile('USGS_LPC_WI_12County_B22_738365.laz', '', 'single_number_split_3')
        model = ProjectedGrid(1000, 1000, 0, 0, 'min', 'min', 4500, 4500,
                              x_ceil_step=4500, y_ceil_step=4500,
                              x_grid_origin=738530 % 4500, y_grid_origin=365990 % 4500)
        self.assertEqual(model.bounds(tile), (738530, 365990, 743030, 370490))
        # The origin shifts the grid, not the filename token. Both alternating
        # truncated X residues must map correctly; padding cannot fix this.
        tile, _ = parse_catalog_tile('USGS_LPC_WI_12County_B22_842545.laz', '', 'single_number_split_3')
        self.assertEqual(model.bounds(tile), (842030, 545990, 846530, 550490))

    def test_grid_origin_preserves_anchor_and_rounding_semantics(self):
        tile, _ = parse_catalog_tile('USGS_LPC_TEST_015027.laz', '', 'single_number_split_3')
        for rounding, expected in [('ceil', (18, 28)), ('floor', (8, 18)), ('round', (18, 28))]:
            model = ProjectedGrid(1, 1, 0, 0, 'min', 'min', 10, 10,
                                  x_grid_origin=8, y_grid_origin=8,
                                  **{f'x_{rounding}_step': 10, f'y_{rounding}_step': 10})
            self.assertEqual(model.bounds(tile)[:2], expected)
        # An origin without a rounding step has no effect on coordinates.
        model = ProjectedGrid(1, 1, 0, 0, 'max', 'center', 10, 10, x_grid_origin=8, y_grid_origin=8)
        self.assertEqual(model.bounds(tile), (5, 22, 15, 32))

    def test_oneida_postfixed_axes_and_sewrpc_prefix(self):
        tile, _ = parse_catalog_tile('USGS_LPC_WI_240E_200N_LAS_2019.laz', '', 'axis_suffix_pair')
        self.assertEqual((tile.x, tile.y), (240, 200))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_WI_240N_200E.laz', '', 'axis_suffix_pair'))
        tile, _ = parse_catalog_tile('USGS_LPC_WI_LD15_2420_340_LAS_2019.laz', '', 'wi_sewrpc_10kft')
        self.assertEqual((tile.x, tile.y), (2420000, 340000))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_WI_2420_340.laz', '', 'wi_sewrpc_10kft'))

    def test_florence_quadrants_and_truncated_corners(self):
        for suffix, expected in [('SW', (330000, 130000)), ('SE', (332500, 130000)),
                                 ('NW', (330000, 132500)), ('NE', (332500, 132500))]:
            tile, _ = parse_catalog_tile('USGS_LPC_WI_03300130' + suffix + '.laz', '', 'wi_2500ft_quadrants')
            self.assertEqual((tile.x, tile.y), expected)
        tile, _ = parse_catalog_tile('USGS_LPC_WI_04000162.laz', '', 'wi_2500ft_quadrants')
        self.assertEqual((tile.x, tile.y), (400000, 162500))
        self.assertIsNone(parse_catalog_tile('USGS_LPC_WI_03300132NE.laz', '', 'wi_2500ft_quadrants'))

    def test_thurston_precision_switch(self):
        for code, expected in [('w10035n53100', (1003500, 531000)),
                               ('w99900n62550', (999000, 625500))]:
            tile, _ = parse_catalog_tile('USGS_LPC_WA_' + code + '.laz', '', 'wa_thurston_4500ft')
            self.assertEqual((tile.x, tile.y), expected)
        self.assertIsNone(parse_catalog_tile('USGS_LPC_WA_e10035n53100.laz', '', 'wa_thurston_4500ft'))

    def test_wv_and_va_letter_grids_have_distinct_origins(self):
        for name, filename, expected in [
            ('WV_FEMA_R3_East_2016', 'USGS_LPC_WV_FF232.laz', (607090.17, 4287379.91)),
            ('VA_FEMA_R3_Northeast_2016', 'USGS_LPC_VA_EH258_LAS_2018.laz', (646090, 4251378)),
        ]:
            item = {'workunit': name, 'project': '', 'horiz_crs': '6346'}
            rule = self.source.catalog.find(item)
            tile = self.source._parse_rule_tile(filename, filename, item, rule, [])
            self.assertEqual(tile.bounds[:2], expected)

    def test_rounded_grid_differs_from_truncated_grid(self):
        # Both directions occur in the same NY workunit; a fixed +/-500
        # offset or padding would hide the error instead of restoring its grid.
        model = ProjectedGrid(1000, 1000, 0, 0, 'min', 'min', 1500, 1500,
                              x_round_step=1500, y_round_step=1500)
        tile, _ = parse_catalog_tile('USGS_LPC_NY_122478.laz', '', 'single_number_split_3')
        self.assertEqual(model.bounds(tile), (121500, 478500, 123000, 480000))
        grid = dict(vars(model), x_ceil_step=1500)
        with self.assertRaisesRegex(ValueError, 'only one grid rounding'):
            LidarProjectCatalog._parse_scheme('test', {'parser': 'single_number_split_3', 'grid': grid})

    def test_real_header_centres_select_one_tile(self):
        names = ('FL_Peninsular_Lake_2018', 'CO_NWCO_2_2020', 'CA_SanFrancisco_1_B23',
                 'NY_FEMA_R2_Northeast_2017', 'NE_Eastern_UA_2016', 'NM_SouthEast_B1_2018',
                 'OH_Statewide_Phase1_2_2019', 'TN_Middle_B1_2018', 'TX_Neches_B5_2016',
                 'TX_LowerRioGrande_2_D22', 'OR_PortlandMetro_1_B24', 'PR_PuertoRicoUSVI_4_D24',
                 'PA_LuzerneCounty_2018', 'VT_Western_2017', 'VA_FEMA_R3_Southwest_A_2016',
                 'SC_SavannahPeeDee_1_2019', 'SC_SavannahPeeDee_2_2019')
        names += ('WI_12County_12_B22', 'WI_StWide_4_StCroix_2021', 'WI_OneidaCo_2013',
                  'WI_Florence_6_2019', 'WI_Brown_2_2020', 'WA_ThurstonCo_1_2021',
                  'WA_CentralWildfire_1_D22', 'WV_FEMA_R3_East_2016', 'VA_FEMA_R3_Northeast_2016')
        for name in names:
            item = next(group for group in self.groups if group['workunit'] == name)
            rule = self.source.catalog.find(item)
            tiles = tuple(self.source._parse_rule_tile(filename, filename, item, rule, []) for filename, _, _ in item['samples'])
            filename, bounds, _ = item['samples'][0]
            x, y = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
            region = Region('test', (x - 0.01, y - 0.01, x + 0.01, y + 0.01), tiles[0].source_crs, {})
            selected = self.source._select_tiles(tiles, {'test'}, {'test': region}, {'test': GeometryCollection()})
            self.assertEqual(list(selected), [filename])

    def test_crs_authority_lookup_is_once_per_scheme(self):
        item = self.groups[0]
        scheme = self.source.catalog.find(item).schemes[0]
        fake_crs = Mock()
        fake_crs.to_string.return_value = 'EPSG:6443'
        with patch.object(self.source, '_workunit_crs', return_value=fake_crs):
            self.source._scheme_crs(scheme, item, None)
            self.source._scheme_crs(scheme, item, None)
        fake_crs.to_string.assert_called_once()

    def test_missing_replay_cache_never_uses_network(self):
        source = CachedAuditSource(project_catalog_path=CATALOG, link_cache_dir=os.path.join(ROOT, 'tests', 'not_a_cache'))
        with patch('geoacquire.sources.usgs.base.requests.get') as network:
            with self.assertRaises(FileNotFoundError):
                source._fetch_text('https://example.com/workunit/0_file_download_links.txt')
            network.assert_not_called()

    def test_known_link_typo_repair_is_idempotent(self):
        url = 'https:/rockyweb.usgs.gov/example.laz'
        fixed = self.source.normalize_link(url)
        self.assertEqual(fixed, 'https://rockyweb.usgs.gov/example.laz')
        self.assertEqual(self.source.normalize_link(fixed), fixed)

    def test_invalid_rounding_is_rejected_at_catalogue_boundary(self):
        grid = dict(x_scale=1, y_scale=1, x_offset=0, y_offset=0,
                    x_anchor='min', y_anchor='min', width=1000, height=1000,
                    x_ceil_step=1000, x_floor_step=1000)
        with self.assertRaisesRegex(ValueError, 'one grid rounding direction'):
            LidarProjectCatalog._parse_scheme('invalid', {'parser': 'mgrs', 'grid': grid})


if __name__ == '__main__':
    unittest.main()
