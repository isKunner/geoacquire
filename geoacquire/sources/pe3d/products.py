#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: products.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: PE3D product catalogue and the deliberately narrow implemented subset.

from dataclasses import dataclass

from geoacquire.core.models import AssetKind, ProductType


@dataclass(frozen=True)
class PE3DProductSpec:
    """Stable product semantics kept separate from the portal protocol."""

    key: str
    site_code: str
    kind: AssetKind
    product: str
    archive_prefix: str | None = None
    scale_path: str | None = None
    archive_directory: str | None = None
    primary_extension: str | None = None
    sidecar_extensions: tuple[str, ...] = ()
    resolution: float | None = None
    implemented: bool = False


# The six portal choices live here so future products can reuse authentication,
# sheet planning, link discovery, and transfer without adding Source classes.
# Archive conventions are filled only after a product has been verified.
PE3D_PRODUCTS: dict[str, PE3DProductSpec] = {
    'orthoimage': PE3DProductSpec('orthoimage', '1', AssetKind.RASTER, ProductType.IMAGERY),
    'dsm_raster': PE3DProductSpec('dsm_raster', '2', AssetKind.RASTER, ProductType.DSM),
    'dsm_xyzi': PE3DProductSpec('dsm_xyzi', '3', AssetKind.POINT_CLOUD, ProductType.DSM),
    'dtm_raster': PE3DProductSpec(
        'dtm_raster',
        '4',
        AssetKind.RASTER,
        ProductType.DTM,
        archive_prefix='MDT-',
        scale_path='1_5000',
        archive_directory='4_MDT_RASTER',
        primary_extension='.tif',
        sidecar_extensions=('.tfw', '.tif.aux.xml'),
        resolution=1.0,
        implemented=True,
    ),
    'dtm_xyz': PE3DProductSpec('dtm_xyz', '5', AssetKind.POINT_CLOUD, ProductType.DTM),
    'intensity_hypsometry': PE3DProductSpec(
        'intensity_hypsometry',
        '6',
        AssetKind.RASTER,
        ProductType.IMAGERY,
    ),
}


def get_product_spec(product: str) -> PE3DProductSpec:
    """Return a known portal product and reject unverified implementations."""
    try:
        spec = PE3D_PRODUCTS[product]
    except KeyError as exc:
        choices = ', '.join(sorted(PE3D_PRODUCTS))
        raise ValueError(f'Unknown PE3D product {product!r}; choose one of: {choices}') from exc
    if not spec.implemented:
        raise ValueError(
            f'PE3D product {product!r} is reserved for future support; '
            "the current implementation downloads only 'dtm_raster'"
        )
    return spec
