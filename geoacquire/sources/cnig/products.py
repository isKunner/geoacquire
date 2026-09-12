#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Reviewed CNIG product contracts."""

from dataclasses import dataclass

from geoacquire.core.models import AssetKind, ProductType


@dataclass(frozen=True)
class CNIGProductSpec:
    key: str
    group_code: str
    series_code: str
    page_slug: str
    filename_prefix: str
    resolution: float
    kind: AssetKind
    product: ProductType


_PRODUCTS = {
    'mdt50cm': CNIGProductSpec(
        key='mdt50cm',
        group_code='MOMDT',
        series_code='MDT01',
        page_slug='modelo-digital-terreno-mdt50cm',
        filename_prefix='MDT50CM-',
        resolution=0.5,
        kind=AssetKind.RASTER,
        product=ProductType.DTM,
    ),
}


def get_product_spec(key: str) -> CNIGProductSpec:
    try:
        return _PRODUCTS[key]
    except KeyError as exc:
        raise ValueError(
            f'Unsupported CNIG product {key!r}; currently available: {sorted(_PRODUCTS)}'
        ) from exc
