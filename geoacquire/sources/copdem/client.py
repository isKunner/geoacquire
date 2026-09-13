#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: client.py
# @Time    : 2026/8/29
# @Author  : Kevin
# @Describe: Minimal stateful CDSE client for Copernicus DEM search, authenticated download, and extraction.

import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta

import requests

from geoacquire.core.models import AssetStatus, validate_path_component
from geoacquire.services.http_stream import is_complete_range, write_response_body


class CDSEAuthError(Exception):
    """CDSE could not establish an authenticated session."""


class CDSEDownloadError(Exception):
    """A catalogue query, product transfer, or archive extraction failed."""


class CopDEMClient:
    """Serial CDSE session; callers own tile retries, this client owns token refresh.

    download_tile returns (AssetStatus, extracted_raster_path, product_id), never
    a ZIP as a final asset. Existing rasters return AssetStatus.SKIPPED.
    """

    AUTH_URL = (
        'https://identity.dataspace.copernicus.eu/'
        'auth/realms/CDSE/protocol/openid-connect/token'
    )
    CATALOG_URL = 'https://catalogue.dataspace.copernicus.eu/odata/v1/Products'
    DOWNLOAD_URL = 'https://download.dataspace.copernicus.eu/odata/v1/Products({product_id})/$value'
    TILE_RE = re.compile(r'([NS])(\d{2})_00_([WE])(\d{3})_00')
    DATASET_VERSION_RE = re.compile(r'/(\d{4})_(\d+)$')

    def __init__(
        self,
        username: str,
        password: str,
        resolution: str = '30',
        dem_format: str = 'DGED',
        filename_suffix: str = 'copdem',
    ):
        if not username or not password:
            raise ValueError('CDSE username and password must not be empty')
        if resolution not in ('30', '90'):
            raise ValueError("resolution must be '30' or '90'")
        if dem_format not in ('DGED', 'DTED'):
            raise ValueError("dem_format must be 'DGED' or 'DTED'")
        validate_path_component(filename_suffix, 'filename_suffix')
        self.username = username
        self.password = password
        self.resolution = resolution
        self.dem_format = dem_format
        self.filename_suffix = filename_suffix
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.session_started_at: datetime | None = None
        self.token_refreshed_at: datetime | None = None
        self.session = requests.Session()

    def authenticate(self) -> None:
        payload = {
            'client_id': 'cdse-public',
            'username': self.username,
            'password': self.password,
            'grant_type': 'password',
        }
        try:
            response = self.session.post(self.AUTH_URL, data=payload, timeout=(30, 30))
            response.raise_for_status()
            data = response.json()
            self.access_token = data['access_token']
            self.refresh_token = data.get('refresh_token')
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise CDSEAuthError(f'CDSE authentication failed: {exc}') from exc
        now = datetime.now()
        self.session_started_at = now
        self.token_refreshed_at = now

    def _refresh_access_token(self) -> None:
        if not self.refresh_token:
            self.authenticate()
            return
        payload = {
            'client_id': 'cdse-public',
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
        }
        try:
            response = self.session.post(self.AUTH_URL, data=payload, timeout=(30, 30))
            response.raise_for_status()
            data = response.json()
            self.access_token = data['access_token']
            self.refresh_token = data.get('refresh_token', self.refresh_token)
            self.token_refreshed_at = datetime.now()
        except (requests.RequestException, KeyError, ValueError):
            self.authenticate()

    def ensure_token(self) -> None:
        if self.access_token is None or self.session_started_at is None or self.token_refreshed_at is None:
            self.authenticate()
            return
        now = datetime.now()
        if now - self.session_started_at >= timedelta(minutes=55):
            self.authenticate()
        elif now - self.token_refreshed_at >= timedelta(minutes=8):
            self._refresh_access_token()

    def search(self, tile_name: str) -> str | None:
        self.ensure_token()
        product_types = {
            ('DGED', '30'): 'SAR_DGE_30_A4AD',
            ('DTED', '30'): 'SAR_DTE_30_615C',
            ('DGED', '90'): 'SAR_DGE_90_A407',
            ('DTED', '90'): 'SAR_DTE_90_61F6',
        }
        product_type = product_types[(self.dem_format, self.resolution)]
        grid_id = self._grid_id(tile_name)
        filters = (
            "Collection/Name eq 'CCM' and "
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'productType' and "
            f"att/OData.CSC.StringAttribute/Value eq '{product_type}') and "
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'gridId' and "
            f"att/OData.CSC.StringAttribute/Value eq '{grid_id}')"
        )
        try:
            response = self.session.get(
                self.CATALOG_URL,
                params={
                    '$filter': filters,
                    '$top': 100,
                    '$select': 'Id,Attributes',
                    '$expand': 'Attributes',
                },
                timeout=(30, 60),
            )
            response.raise_for_status()
            values = response.json().get('value', [])
        except (requests.RequestException, ValueError) as exc:
            raise CDSEDownloadError(f'CDSE catalogue query failed for {tile_name}: {exc}') from exc
        candidates: list[tuple[int, int, str]] = []
        for item in values:
            product_id = str(item.get('Id') or '')
            if not product_id:
                continue
            year, delivery = self._dataset_version(item.get('Attributes', ()))
            candidates.append((year, delivery, product_id))
        if not candidates:
            return None
        # The latest delivery contains the current corrections. UUID is the
        # deterministic tie-breaker if CDSE ever publishes duplicate records
        # within one delivery, so retries always address the same .part file.
        return min(candidates, key=lambda item: (-item[0], -item[1], item[2]))[2]

    def download_product(self, product_id: str, destination: str, chunk_size: int = 1024 * 1024) -> None:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        part_path = destination + '.part'
        for auth_attempt in range(2):
            self.ensure_token()
            resume_byte = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
            headers = {'Authorization': f'Bearer {self.access_token}', 'Accept-Encoding': 'identity'}
            if resume_byte > 0:
                headers['Range'] = f'bytes={resume_byte}-'
            response = self.session.get(
                self.DOWNLOAD_URL.format(product_id=product_id),
                headers=headers,
                stream=True,
                timeout=(30, 600),
            )
            if response.status_code == 401 and auth_attempt == 0:
                response.close()
                self.authenticate()
                continue
            try:
                if not is_complete_range(response, resume_byte):
                    self._raise_download_error(response, product_id)
                    write_response_body(response, part_path, resume_byte, chunk_size)
                os.replace(part_path, destination)
                return
            finally:
                response.close()
        raise CDSEDownloadError(f'CDSE authorization failed twice for product {product_id}')

    @staticmethod
    def _raise_download_error(response: requests.Response, product_id: str) -> None:
        if response.status_code == 403:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise CDSEDownloadError(
                f'CDSE refused product {product_id}. Confirm that CCM access and the ESA CCM license '
                f'are enabled for the account. Response: {body}'
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CDSEDownloadError(f'CDSE download failed for {product_id}: {exc}') from exc
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            raise CDSEDownloadError(f'CDSE returned HTML for product {product_id}')

    def download_tile(
        self,
        tile_name: str,
        output_dir: str,
        skip_existing: bool = True,
        keep_archive: bool = False,
        chunk_size: int = 1024 * 1024,
        existing_filenames: frozenset[str] | None = None,
    ) -> tuple[AssetStatus, str, str]:
        os.makedirs(output_dir, exist_ok=True)
        existing = self._find_existing(
            tile_name,
            output_dir,
            self.filename_suffix,
            existing_filenames,
        )
        if skip_existing and existing:
            return AssetStatus.SKIPPED, existing, ''

        product_id = self.search(tile_name)
        if product_id is None:
            raise CDSEDownloadError(f'No CDSE product found for tile {tile_name}')
        archive_path = os.path.join(output_dir, f'{product_id}.zip')
        if not self._valid_archive(archive_path):
            self.download_product(product_id, archive_path, chunk_size)
        extracted = self.extract(
            archive_path,
            tile_name,
            output_dir,
            self.filename_suffix,
        )
        if not keep_archive:
            os.remove(archive_path)
        return AssetStatus.SUCCESS, extracted, product_id

    @staticmethod
    def _find_existing(
        tile_name: str,
        output_dir: str,
        filename_suffix: str,
        existing_filenames: frozenset[str] | None = None,
    ) -> str | None:
        filenames = existing_filenames
        if filenames is None:
            filenames = CopDEMClient.scan_filenames(output_dir)
        for extension in ('.tif', '.tiff', '.dt1', '.dt2'):
            path = os.path.join(output_dir, f'{tile_name}_{filename_suffix}{extension}')
            if os.path.normcase(os.path.basename(path)) in filenames:
                return path
            legacy = os.path.join(output_dir, tile_name + extension)
            if os.path.normcase(os.path.basename(legacy)) in filenames:
                os.replace(legacy, path)
                return path
        return None

    @staticmethod
    def scan_filenames(output_dir: str) -> frozenset[str]:
        """Read one flat output directory into an exact basename snapshot."""
        try:
            names: set[str] = set()
            with os.scandir(output_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_file():
                            names.add(os.path.normcase(entry.name))
                    except OSError:
                        continue
            return frozenset(names)
        except FileNotFoundError:
            return frozenset()

    @staticmethod
    def _valid_archive(path: str) -> bool:
        if not os.path.isfile(path):
            return False
        try:
            with zipfile.ZipFile(path, 'r') as archive:
                return archive.testzip() is None
        except zipfile.BadZipFile:
            return False

    @staticmethod
    def extract(
        archive_path: str,
        tile_name: str,
        output_dir: str,
        filename_suffix: str = 'copdem',
    ) -> str:
        with zipfile.ZipFile(archive_path, 'r') as archive:
            candidates = []
            for member in archive.namelist():
                lower = member.lower()
                if any(tag in lower for tag in ('preview', 'quicklook', '/aux/', 'overview')):
                    continue
                if lower.endswith(('dem.tif', 'dem.tiff', 'dem.dt1', 'dem.dt2')):
                    candidates.append(member)
            if not candidates:
                raise CDSEDownloadError(f'No DEM raster found in archive: {archive_path}')
            candidates.sort(key=lambda member: (tile_name.lower() not in member.lower(), member.count('/')))
            member = candidates[0]
            extension = os.path.splitext(os.path.basename(member))[1]
            filename = f'{tile_name}_{filename_suffix}{extension}'
            destination = os.path.join(output_dir, filename)
            temporary = destination + '.part'
            try:
                with archive.open(member) as source, open(temporary, 'wb') as target:
                    shutil.copyfileobj(source, target)
                os.replace(temporary, destination)
            finally:
                if os.path.isfile(temporary):
                    os.remove(temporary)
            return destination

    @classmethod
    def _grid_id(cls, tile_name: str) -> str:
        match = cls.TILE_RE.search(tile_name)
        if not match:
            raise ValueError(f'Invalid CopDEM tile name: {tile_name}')
        latitude_direction, latitude_value, longitude_direction, longitude_value = match.groups()
        return (
            f'{latitude_direction}{latitude_value}_'
            f'{longitude_direction}{longitude_value}'
        )

    @classmethod
    def _dataset_version(cls, attributes) -> tuple[int, int]:
        for attribute in attributes:
            if attribute.get('Name') != 'dataset':
                continue
            match = cls.DATASET_VERSION_RE.search(str(attribute.get('Value') or ''))
            if match:
                return int(match.group(1)), int(match.group(2))
        return -1, -1
