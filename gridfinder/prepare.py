# gridfinder
# Licensed under GPL-3.0
# (C) Christopher Arderne

import os
from math import sqrt
import json
from pathlib import Path

import numpy as np
from scipy import signal

import rasterio
from rasterio.mask import mask
from rasterio.features import shapes, rasterize
from rasterio import Affine
from rasterio.warp import reproject, Resampling

import geopandas as gpd
from gridfinder._util import clip_line_poly, save_raster, clip_raster


def clip_rasters(folder_in, folder_out, aoi_in):
    """
    Read continental rasters one at a time, clip and save
    """

    if isinstance(aoi_in, gpd.GeoDataFrame):
        aoi = aoi_in
    else:
        aoi = gpd.read_file(aoi_in)

    coords = [json.loads(aoi.to_json())['features'][0]['geometry']]

    for file in os.listdir(folder_in):
        if file.endswith('.tif'):
            print(f'Doing {file}')
            ntl_rd = rasterio.open(os.path.join(folder_in, file))
            ntl, affine = mask(dataset=ntl_rd, shapes=coords, crop=True, nodata=0)

            if ntl.ndim == 3:
                ntl = ntl[0]
                
            save_raster(folder_out / file, ntl, affine)


def merge_rasters(folder, percentile=70):
    """
    Merge a set of monthly rasters keeping the nth percentile value.
    Used to remove transient features from time-series data.
    """

    affine = None
    rasters = []

    for file in os.listdir(folder):
        if file.endswith('.tif'):
            ntl_rd = rasterio.open(os.path.join(folder, file))
            rasters.append(ntl_rd.read(1))
            
            if not affine:
                affine = ntl_rd.transform

    raster_arr = np.array(rasters)

    raster_merged = np.nanpercentile(raster_arr, percentile, axis=0)

    return raster_merged, affine


def filter_func(i, j):
    """

    """

    d_rows = abs(i - 20)
    d_cols = abs(j - 20)
    d = sqrt(d_rows**2 + d_cols**2)
    
    if i == 20 and j == 20:
        return 0
    elif d <= 20: 
        return 1 / (1 + d/2)**3
    else:
        return 0.0

def create_filter():
    """

    """
    vec_filter_func = np.vectorize(filter_func)
    ntl_filter = np.fromfunction(vec_filter_func, (41, 41), dtype=float)

    ntl_filter = ntl_filter / ntl_filter.sum()

    return ntl_filter


def prepare_ntl(ntl_in, aoi_in, ntl_filter=None, threshold=0.1, upsample_by=3):
    """

    """

    if isinstance(aoi_in, gpd.GeoDataFrame):
        aoi = aoi_in
    else:
        aoi = gpd.read_file(aoi_in)

    if ntl_filter is None:
        ntl_filter = create_filter()

    ntl_big = rasterio.open(ntl_in)

    coords = [json.loads(aoi.to_json())['features'][0]['geometry']]
    ntl, affine = mask(dataset=ntl_big, shapes=coords, crop=True, nodata=0)

    if ntl.ndim == 3:
        ntl = ntl[0]

    ntl_convolved = signal.convolve2d(ntl, ntl_filter, mode='same')
    ntl_filtered = ntl - ntl_convolved

    ntl_interp = np.empty(shape=(1,  # same number of bands
                                round(ntl.shape[0] * upsample_by),
                                round(ntl.shape[1] * upsample_by)))

    # adjust the new affine transform to the 150% smaller cell size
    newaff = Affine(affine.a / upsample_by, affine.b, affine.c,
                        affine.d, affine.e / upsample_by, affine.f)

    with rasterio.Env():
        reproject(
            ntl_filtered, ntl_interp,
            src_transform = affine,
            dst_transform = newaff,
            src_crs = {'init': 'epsg:4326'},
            dst_crs = {'init': 'epsg:4326'},
            resampling = Resampling.bilinear)
        
    ntl_interp = ntl_interp[0]

    ntl_thresh = np.empty_like(ntl_interp)
    ntl_thresh[:] = ntl_interp[:]
    ntl_thresh[ntl_thresh < threshold] = 0
    ntl_thresh[ntl_thresh >= threshold] = 1

    return ntl, ntl_filtered, ntl_interp, ntl_thresh, newaff


def drop_zero_pop(targets_in, pop_in, aoi):
    """

    """

    if isinstance(aoi, (str, Path)):
        aoi = gpd.read_file(aoi)

    # Clip population layer to AOI
    clipped, affine, crs = clip_raster(pop_in, aoi)
    clipped = clipped[0]

    # We need to warp the population layer to exactly overlap cell for cell with targets
    # First get array, affine and crs from targets (which is what we)
    targets_rd = rasterio.open(targets_in)
    targets = targets_rd.read(1)
    ghs_proj = np.empty_like(targets)
    dest_affine = targets_rd.transform
    dest_crs = targets_rd.crs

    # Then use reproject 
    with rasterio.Env():
        reproject(
            source = clipped, 
            destination = ghs_proj,
            src_transform = affine,
            dst_transform = dest_affine,
            src_crs = crs,
            dst_crs = dest_crs,
            resampling = Resampling.bilinear)

    # Finally read to run algorithm to drop target blobs (continuous areas of target==1)
    # where there is no underlying population

    blobs = []
    skip = []
    max_i = targets.shape[0]
    max_j = targets.shape[1]

    def add_around(blob, cell):
        blob.append(cell)
        skip.append(cell)
        
        for x in range(-1,2):
            for y in range(-1,2):
                next_i = i + x
                next_j = j + y
                next_cell = (next_i, next_j)
                
                # ensure we're within bounds
                if next_i >= 0 and next_j >= 0 and next_i < max_i and next_j < max_j:
                    # ensure we're not looking at the same spot or one that's been done
                    if not next_cell == cell and next_cell not in skip:
                        # if it's an electrified cell
                        if targets[next_i][next_j] == 1:
                            blob = add_around(blob, next_cell)

        return blob

    for i in range(max_i):
        for j in range(max_j):
            if targets[i][j] == 1 and (i, j) not in skip:          
                blob = add_around(blob=[], cell=(i, j))
                blobs.append(blob)
                
    for blob in blobs:
        found = False
        for cell in blob:
            if ghs_proj[cell] > 1:
                found = True
                break
        if not found:
            # set all values in blob to 0
            for cell in blob:
                targets[cell] = 0

    return targets




def prepare_roads(roads_in, aoi_in, ntl_in):
    """
    
    """

    ntl_rd = rasterio.open(ntl_in)
    shape = ntl_rd.read(1).shape
    affine = ntl_rd.transform

    if isinstance(aoi_in, gpd.GeoDataFrame):
        aoi = aoi_in
    else:
        aoi = gpd.read_file(aoi_in)

    roads = gpd.read_file(roads_in)

    roads['weight'] = 1
    roads.loc[roads['highway'] == 'motorway', 'weight'] = 1/10
    roads.loc[roads['highway'] == 'trunk', 'weight'] = 1/9
    roads.loc[roads['highway'] == 'primary', 'weight'] = 1/8
    roads.loc[roads['highway'] == 'secondary', 'weight'] = 1/7
    roads.loc[roads['highway'] == 'tertiary', 'weight'] = 1/6
    roads.loc[roads['highway'] == 'unclassified', 'weight'] = 1/5
    roads.loc[roads['highway'] == 'residential', 'weight'] = 1/4
    roads.loc[roads['highway'] == 'service', 'weight'] = 1/3

    roads = roads[roads.weight != 1]

    roads_clipped = clip_line_poly(roads, aoi)

    # sort by weight descending
    # so that lower weight (bigger roads) are processed last and overwrite higher weight roads
    roads_clipped = roads_clipped.sort_values(by='weight', ascending=False)

    roads_for_raster = [(row.geometry, row.weight) for _, row in roads_clipped.iterrows()]
    roads_raster = rasterize(roads_for_raster, out_shape=shape, fill=1,
                         default_value=0, all_touched=True, transform=affine)

    return roads, roads_clipped, aoi, roads_raster, affine