#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: client.py
# @Time    : 2026/9/11
# @Author  : Kevin
# @Describe: Stateful PE3D login, CAPTCHA, link discovery, and safe archive extraction.

import os
import re
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

import requests

from geoacquire.core.models import validate_path_component

from .catalog import PE3DQuadrangle
from .products import PE3DProductSpec
from .tls import resolve_tls_verification


class PE3DAuthError(RuntimeError):
    """PE3D could not establish an authenticated session."""


class PE3DProtocolError(RuntimeError):
    """PE3D returned an invalid or unexpected catalogue response."""


@dataclass(frozen=True)
class PE3DDownload:
    sheet_code: str
    url: str
    filename: str


class _IframeSourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != 'iframe':
            return
        for name, value in attrs:
            if name.lower() == 'src' and value:
                self.sources.append(value)


class PE3DClient:
    """One portal session shared by CAPTCHA login and quadrangle discovery."""

    BASE_URL = 'https://pe3d.pe.gov.br/'
    MAP_URL = BASE_URL + 'mapa.php'
    LOGIN_URL = BASE_URL + 'login.php'
    CAPTCHA_URL = BASE_URL + 'get_captcha.php'
    DOWNLOAD_LIST_URL = BASE_URL + 'baixararquivo.php'
    USER_AGENT = 'Mozilla/5.0 (compatible; GeoAcquire/0.1)'
    SHEET_RE = re.compile(
        r'^S[A-Z]-\d{2}-[VXYZ]-[ABCD]-(?:I|II|III|IV|V|VI)-[1-4]-'
        r'(?:NO|NE|SO|SE)-[A-F]-(?:I|II|III|IV)$'
    )

    def __init__(
        self,
        username: str,
        password: str,
        captcha_path: str = './cache/pe3d/captcha.png',
        auth_attempts: int = 3,
        batch_size: int = 100,
        verify_tls: bool = True,
        ca_bundle_path: str | None = None,
        quadrangle_mode: str = 'quad',
        session: requests.Session | None = None,
    ):
        if not username or not password:
            raise ValueError('PE3D username and password must not be empty')
        if not captcha_path:
            raise ValueError('captcha_path must not be empty')
        if auth_attempts < 1:
            raise ValueError('auth_attempts must be >= 1')
        if batch_size < 1:
            raise ValueError('batch_size must be >= 1')
        if not quadrangle_mode:
            raise ValueError('quadrangle_mode must not be empty')
        self.username = username
        self.password = password
        self.captcha_path = captcha_path
        self.auth_attempts = auth_attempts
        self.batch_size = batch_size
        self.verify_tls = resolve_tls_verification(verify_tls, ca_bundle_path)
        self.quadrangle_mode = quadrangle_mode
        self.session = session or requests.Session()
        self.authenticated = False

    def authenticate(self, captcha_reader: Callable[[str], str] | None = None) -> None:
        """Prime the session and ask the local operator to read each CAPTCHA."""
        reader = captcha_reader or self._prompt_captcha
        headers = {'User-Agent': self.USER_AGENT}
        try:
            response = self.session.get(
                self.BASE_URL,
                headers=headers,
                verify=self.verify_tls,
                timeout=(30, 60),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PE3DAuthError(f'PE3D session initialization failed: {exc}') from exc

        for _ in range(self.auth_attempts):
            self._write_captcha(headers)
            try:
                answer = reader(os.path.abspath(self.captcha_path)).strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise PE3DAuthError('PE3D CAPTCHA input was cancelled') from exc
            if not answer:
                continue
            payload = {
                'tela': 'login',
                'usuario': self.username,
                'senha': self.password,
                'captcha': answer,
            }
            login_headers = {
                **headers,
                'Referer': self.MAP_URL,
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': self.BASE_URL.rstrip('/'),
            }
            try:
                response = self.session.post(
                    self.LOGIN_URL,
                    data=payload,
                    headers=login_headers,
                    verify=self.verify_tls,
                    timeout=(30, 60),
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise PE3DAuthError(f'PE3D login request failed: {exc}') from exc
            if response.text.strip() == 'ok':
                self.authenticated = True
                self._remove_captcha()
                return
        raise PE3DAuthError(
            f'PE3D login failed after {self.auth_attempts} CAPTCHA attempt(s); '
            'the latest image remains at the configured captcha_path'
        )

    def list_downloads(
        self,
        quadrangles: Iterable[PE3DQuadrangle],
        product: PE3DProductSpec,
    ) -> tuple[PE3DDownload, ...]:
        """Resolve verified 1:5,000 archive links for selected quadrangles."""
        if not self.authenticated:
            raise PE3DAuthError('PE3D link discovery requires authentication')
        if not product.archive_prefix or not product.scale_path or not product.archive_directory:
            raise PE3DProtocolError(f'PE3D product {product.key!r} has no verified archive convention')
        selections = tuple({item.sheet_code: item for item in quadrangles}.values())
        if not selections:
            return ()
        codes = tuple(item.sheet_code for item in selections)
        for code in codes:
            if not self.SHEET_RE.fullmatch(code):
                raise ValueError(f'Invalid PE3D 1:5,000 sheet code: {code!r}')

        payload: list[tuple[str, str]] = [('tipo', product.site_code)]
        payload.extend(('id[]', item.selection_value) for item in selections)
        payload.extend((('id[]', ''), ('mun_quad', self.quadrangle_mode), ('timeout', '5000')))
        headers = {
            'User-Agent': self.USER_AGENT,
            'Referer': self.MAP_URL,
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': self.BASE_URL.rstrip('/'),
        }
        try:
            response = self.session.post(
                self.DOWNLOAD_LIST_URL,
                data=payload,
                headers=headers,
                verify=self.verify_tls,
                timeout=(30, 120),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PE3DProtocolError(f'PE3D link discovery failed: {exc}') from exc

        parser = _IframeSourceParser()
        parser.feed(response.text)
        requested = set(codes)
        by_sheet: dict[str, PE3DDownload] = {}
        for source in parser.sources:
            download = self._parse_download(source, product, requested)
            if download is None:
                continue
            previous = by_sheet.get(download.sheet_code)
            if previous is not None and previous.url != download.url:
                raise PE3DProtocolError(
                    f'PE3D returned multiple 1:5,000 archives for sheet {download.sheet_code}'
                )
            by_sheet[download.sheet_code] = download
        return tuple(by_sheet[code] for code in codes if code in by_sheet)

    def download_headers(self) -> dict[str, str]:
        """Copy authenticated cookies into independent HTTP worker sessions."""
        if not self.authenticated:
            raise PE3DAuthError('PE3D download headers require authentication')
        cookies = requests.utils.dict_from_cookiejar(self.session.cookies)
        headers = {'User-Agent': self.USER_AGENT, 'Referer': self.MAP_URL}
        if cookies:
            headers['Cookie'] = '; '.join(f'{name}={value}' for name, value in cookies.items())
        return headers

    @staticmethod
    def scan_filenames(output_dir: str) -> frozenset[str]:
        try:
            with os.scandir(output_dir) as entries:
                return frozenset(
                    os.path.normcase(entry.name)
                    for entry in entries
                    if entry.is_file()
                )
        except FileNotFoundError:
            return frozenset()

    @staticmethod
    def extract_archive(
        archive_path: str,
        sheet_code: str,
        output_dir: str,
        product: PE3DProductSpec,
    ) -> str:
        """Extract one product asset and optional sidecars without zip-slip risk."""
        if not product.archive_prefix or not product.primary_extension:
            raise PE3DProtocolError(f'PE3D product {product.key!r} has no extraction convention')
        expected_stem = f'{product.archive_prefix}{sheet_code}'
        primary_extension = product.primary_extension.lower()
        allowed_suffixes = (*product.sidecar_extensions, primary_extension)
        temporary_paths: list[str] = []
        prepared: list[tuple[str, str]] = []
        try:
            with zipfile.ZipFile(archive_path, 'r') as archive:
                selected: dict[str, str] = {}
                for member in archive.namelist():
                    basename = os.path.basename(member.replace('\\', '/'))
                    lower = basename.lower()
                    if not basename or not lower.startswith(expected_stem.lower()):
                        continue
                    suffix = lower[len(expected_stem):]
                    if suffix not in allowed_suffixes:
                        continue
                    if suffix in selected:
                        raise PE3DProtocolError(
                            f'Duplicate {suffix} member in PE3D archive: {archive_path}'
                        )
                    selected[suffix] = member
                if primary_extension not in selected:
                    raise PE3DProtocolError(
                        f'No {expected_stem}{primary_extension} asset found in PE3D archive: '
                        f'{archive_path}'
                    )

                os.makedirs(output_dir, exist_ok=True)
                # Sidecars are published first and the primary asset last.
                for suffix in (*product.sidecar_extensions, primary_extension):
                    member = selected.get(suffix)
                    if member is None:
                        continue
                    destination = os.path.join(output_dir, expected_stem + suffix)
                    validate_path_component(os.path.basename(destination), 'PE3D extracted filename')
                    temporary = destination + '.part'
                    temporary_paths.append(temporary)
                    with archive.open(member) as source, open(temporary, 'wb') as target:
                        shutil.copyfileobj(source, target)
                    prepared.append((temporary, destination))

            for temporary, destination in prepared:
                os.replace(temporary, destination)
                temporary_paths.remove(temporary)
            return os.path.join(output_dir, expected_stem + primary_extension)
        except zipfile.BadZipFile as exc:
            raise PE3DProtocolError(f'Invalid PE3D ZIP archive: {archive_path}') from exc
        finally:
            for temporary in temporary_paths:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass

    def _write_captcha(self, headers: dict[str, str]) -> None:
        try:
            response = self.session.get(
                self.CAPTCHA_URL,
                headers=headers,
                verify=self.verify_tls,
                timeout=(30, 60),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PE3DAuthError(f'PE3D CAPTCHA request failed: {exc}') from exc
        directory = os.path.dirname(os.path.abspath(self.captcha_path))
        os.makedirs(directory, exist_ok=True)
        temporary = self.captcha_path + '.part'
        try:
            with open(temporary, 'wb') as stream:
                stream.write(response.content)
            os.replace(temporary, self.captcha_path)
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)

    @staticmethod
    def _prompt_captcha(path: str) -> str:
        return input(f'PE3D CAPTCHA saved to {path}. Open it and enter the text: ')

    def _remove_captcha(self) -> None:
        try:
            os.remove(self.captcha_path)
        except FileNotFoundError:
            pass

    def _parse_download(
        self,
        source: str,
        product: PE3DProductSpec,
        requested: set[str],
    ) -> PE3DDownload | None:
        url = urljoin(self.BASE_URL, source)
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'pe3d.pe.gov.br', 'www.pe3d.pe.gov.br'}:
            raise PE3DProtocolError(f'PE3D returned an untrusted download URL: {url}')
        decoded_path = unquote(parsed.path)
        path_parts = {part.lower() for part in decoded_path.split('/') if part}
        if product.scale_path.lower() not in path_parts:
            return None
        if product.archive_directory.lower() not in path_parts:
            return None
        filename = os.path.basename(decoded_path)
        validate_path_component(filename, 'PE3D archive filename')
        if not filename.lower().endswith('.zip'):
            return None
        stem = filename[:-4]
        if not stem.lower().startswith(product.archive_prefix.lower()):
            return None
        sheet_code = stem[len(product.archive_prefix):]
        if sheet_code not in requested:
            return None
        return PE3DDownload(sheet_code, url, filename)
