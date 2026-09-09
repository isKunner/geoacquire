#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName: fiexd_lidar_dsm
# @Time    : 2026/9/2 11:13
# @Author  : Kevin
# @Describe: After downloading the USGS LiDAR data and extracting the DSM,
# we found that a substantial number of NoData pixels remained.
# Visual inspection showed that filling these missing areas with
# the corresponding USGS 1 m DTM produced no noticeable discontinuities,
# as most of the missing regions were located over bare ground.
# Therefore, despite the difference in vertical reference systems between the two datasets,
# we directly used the 1 m DTM elevations to fill the NoData areas in the DSM.

import os

import numpy as np
import rasterio


ROOT = r"/mnt/disk/USA/SurfRef_Data/USA_GeoDAR/GT/"


def check_alignment(src, lidar, base_path, lidar_path):
    if src.width != lidar.width or src.height != lidar.height:
        raise ValueError(
            f"Shape mismatch:\n"
            f"  Base : {base_path} -> {src.width} x {src.height}\n"
            f"  LiDAR: {lidar_path} -> {lidar.width} x {lidar.height}"
        )

    if src.transform != lidar.transform:
        raise ValueError(
            f"Transform mismatch:\n"
            f"  Base : {base_path}\n"
            f"  LiDAR: {lidar_path}"
        )


def get_nodata_mask(dataset, data):
    mask = dataset.read_masks(1) == 0
    mask |= ~np.isfinite(data)

    nodata = dataset.nodata
    if nodata is not None:
        if np.isnan(nodata):
            mask |= np.isnan(data)
        else:
            mask |= data == nodata

    return mask


def build_dsm(base_path, lidar_path, dsm_path):
    with rasterio.open(base_path) as src, rasterio.open(lidar_path) as lidar:
        check_alignment(src, lidar, base_path, lidar_path)

        if src.count != 1 or lidar.count != 1:
            raise ValueError(f"Only single-band TIFF is supported:\n  {base_path}\n  {lidar_path}")

        base = src.read(1)
        lidar_data = lidar.read(1)

        lidar_mask = get_nodata_mask(lidar, lidar_data)
        base_mask = get_nodata_mask(src, base)

        fill_mask = lidar_mask & ~base_mask

        dsm = lidar_data.copy()
        dsm[fill_mask] = base[fill_mask]

        profile = lidar.profile.copy()
        fill_count = int(fill_mask.sum())
        total_count = lidar.width * lidar.height
        unfilled_count = int((lidar_mask & base_mask).sum())

    with rasterio.open(dsm_path, "w", **profile) as dst:
        dst.write(dsm, 1)

    return fill_count, total_count, unfilled_count


def process_folder(folder):
    filenames = os.listdir(folder)
    tif_names = {name for name in filenames if name.lower().endswith(".tif")}

    base_names = [
        name for name in tif_names
        if not name.lower().endswith("_lidar.tif")
        and not name.lower().endswith("_dsm.tif")
    ]

    pair_count = 0
    total_filled = 0
    total_pixels = 0
    total_unfilled = 0
    fill_ratios = []

    for base_name in sorted(base_names):
        stem = os.path.splitext(base_name)[0]
        lidar_name = f"{stem}_lidar.tif"

        if lidar_name not in tif_names:
            continue

        base_path = os.path.join(folder, base_name)
        lidar_path = os.path.join(folder, lidar_name)
        dsm_path = os.path.join(folder, f"{stem}_dsm.tif")

        filled, pixels, unfilled = build_dsm(base_path, lidar_path, dsm_path)

        pair_count += 1
        total_filled += filled
        total_pixels += pixels
        total_unfilled += unfilled
        fill_ratios.append(filled / pixels * 100)

    avg_fill_ratio = float(np.mean(fill_ratios)) if fill_ratios else 0.0
    return pair_count, total_filled, total_pixels, total_unfilled, avg_fill_ratio


def main():
    total_pairs = 0
    total_filled = 0
    total_pixels = 0
    total_unfilled = 0
    folder_ratios = []

    for name in sorted(os.listdir(ROOT)):
        folder = os.path.join(ROOT, name)
        if not os.path.isdir(folder):
            continue

        pairs, filled, pixels, unfilled, avg_ratio = process_folder(folder)

        if pairs == 0:
            continue

        total_pairs += pairs
        total_filled += filled
        total_pixels += pixels
        total_unfilled += unfilled
        folder_ratios.append(avg_ratio)

        print(
            f"{name}: "
            f"{pairs} pairs | "
            f"avg filled {avg_ratio:.2f}% | "
            f"filled pixels {filled:,} | "
            f"still NoData {unfilled:,}"
        )

    overall_ratio = total_filled / total_pixels * 100 if total_pixels else 0.0
    avg_folder_ratio = float(np.mean(folder_ratios)) if folder_ratios else 0.0

    print("\n" + "=" * 70)
    print(f"Total pairs: {total_pairs}")
    print(f"Total filled pixels: {total_filled:,}")
    print(f"Overall filled area: {overall_ratio:.2f}%")
    print(f"Average folder fill ratio: {avg_folder_ratio:.2f}%")
    print(f"Total still NoData pixels: {total_unfilled:,}")


if __name__ == "__main__":
    main()