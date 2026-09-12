#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: tls.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: PE3D-specific verified CA chain for the portal's incomplete TLS response.

import os


_PACKAGED_CA_BUNDLE = os.path.join(
    os.path.dirname(__file__),
    'certs',
    'pe3d_zerossl_chain.pem',
)


def resolve_tls_verification(
    verify_tls: bool,
    ca_bundle_path: str | None = None,
) -> bool | str:
    """Return the Requests verify value for all PE3D HTTPS operations.

    PE3D currently sends only its leaf certificate. Browsers complete the
    missing issuer through the operating-system certificate store, while
    Requests/OpenSSL does not. The packaged bundle pins the public ZeroSSL
    intermediate and Sectigo root used by the portal, preserving hostname,
    validity-period, signature, and chain verification.
    """
    if not verify_tls:
        return False
    path = ca_bundle_path or _PACKAGED_CA_BUNDLE
    absolute = os.path.abspath(path)
    if not os.path.isfile(absolute):
        raise ValueError(f'PE3D TLS CA bundle does not exist: {absolute}')
    return absolute
