# coding: utf-8

"""
Valley Bottom Delineation Refinement Procedure

***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 3 of the License, or     *
*   (at your option) any later version.                                   *
*                                                                         *
***************************************************************************
"""

import os
from operator import itemgetter
import itertools
from multiprocessing import Pool

import numpy as np
from scipy.interpolate import interp1d

import click
import xarray as xr
import rasterio as rio
from rasterio import features
import fiona

from .. import transform as fct
from .. import speedup
from ..tileio import as_window
from ..rasterize import rasterize_linestring
from ..config import config
from ..cli import starcall

def TalwegHeightBySwathUnit(axis):
    """
    Calculate median talweg height relative to valley floor
    """

    elevation_raster = config.tileset().filename('dem')
    talweg_shapefile = config.filename('ax_talweg', axis=axis)
    swath_bounds = config.filename('ax_valley_swaths_bounds', axis=axis)
    swath_raster = config.tileset().filename('ax_valley_swaths', axis=axis)
    measure_raster = config.tileset().filename('ax_axis_measure', axis=axis)

    # swath => z0, slope

    defs = xr.open_dataset(swath_bounds)
    defs.load()
    defs = defs.sortby('coordm')

    estimates = dict()

    with click.progressbar(defs['label'].values) as iterator:
        for gid in iterator:

            filename = config.filename('ax_swath_elevation', axis=axis, gid=gid)

            if os.path.exists(filename):

                data = np.load(filename, allow_pickle=True)
                z0 = data['z0_valley_floor']
                slope = data['slope_valley_floor']

                if not (np.isnan(z0) or np.isnan(slope)):
                    estimates[gid] = (slope, z0)

    # talweg => vertices (x, y, z, swath, axis m)

    swathid = np.array([])
    coordz = np.array([])
    coordm = np.array([])
    # coordxy = np.zeros((0, 2), dtype='float32')

    with fiona.open(talweg_shapefile) as fs:
        with click.progressbar(fs, length=len(fs)) as iterator:
            for feature in iterator:

                coordinates = np.array(feature['geometry']['coordinates'], dtype='float32')

                with rio.open(swath_raster) as ds:

                    coordij = fct.worldtopixel(coordinates[:, :2], ds.transform)
                    pixels = list()

                    # we must interpolate segments between vertices
                    # otherwise we may miss out swaths that fit between 2 vertices

                    for a, b in zip(coordij[:-1], coordij[1:]):
                        for i, j in rasterize_linestring(a, b):
                            pixels.append((i, j))

                    segment_xy = fct.pixeltoworld(
                        np.array(pixels, dtype='int32'),
                        ds.transform)

                    # coordxy = np.concatenate([
                    #     coordxy,
                    #     segment_xy
                    # ], axis=0)

                    segment_swathid = np.array(list(ds.sample(segment_xy, 1)))
                    swathid = np.concatenate([swathid, segment_swathid[:, 0]], axis=0)

                with rio.open(measure_raster) as measure_ds:

                    segment_m = np.array(list(measure_ds.sample(segment_xy, 1)))
                    coordm = np.concatenate([coordm, segment_m[:, 0]], axis=0)

                with rio.open(elevation_raster) as ds:

                    segment_z = np.array(list(ds.sample(segment_xy, 1)))
                    coordz = np.concatenate([coordz, segment_z[:, 0]], axis=0)

    indices = sorted(enumerate(swathid), key=itemgetter(1))
    groups = itertools.groupby(indices, key=itemgetter(1))
    values = list()
    swids = list()

    for swid, group in groups:

        if swid == 0:
            continue

        elements = np.array([k for k, _ in group])

        if swid in estimates:

            slope, z0 = estimates[swid]

            ztalweg = coordz[elements]
            zvalley = slope * coordm[elements] + z0

            hmedian = np.median(ztalweg - zvalley)
            hmin = np.min(ztalweg - zvalley)

        else:

            # htalweg = -10.0
            hmedian = hmin = np.nan

        swathm = defs['coordm'].sel(label=swid).values
        values.append((swathm, hmedian, hmin))
        swids.append(swid)

    return (
        np.array(swids, dtype='uint32'),
        np.array(values, dtype='float32')
    )

def InterpolateMissingValues(values, kind='slinear'):
    """
    """

    isnan = np.isnan(values[:, 1])
    notnan = ~isnan

    missing = np.copy(values[:, 1:])

    for k in range(2):
        fun = interp1d(values[notnan, 0], values[notnan, k+1], kind=kind, bounds_error=False)
        missing[isnan, k] = fun(values[isnan, 0])

    return missing

def WriteTalwegHeights(axis, swaths, values):
    """
    Save talweg height data to NetCDF file
    """

    output = config.filename('metrics_talweg_height', axis=axis)
    interpolated = np.isnan(values[:, 1])
    fixed = InterpolateMissingValues(values)

    dataset = xr.Dataset(
        {
            'hmed': ('swath', fixed[:, 0]),
            'hmin': ('swath', fixed[:, 1]),
            'interp': ('swath', interpolated)
        },
        coords={
            'axis': axis,
            'swath': swaths,
            'measure': ('swath', values[:, 0]),
        }
    )

    # Metadata

    dataset['hmed'].attrs['long_name'] = 'median height relative to valley floor'
    dataset['hmed'].attrs['units'] = 'm'

    dataset['hmin'].attrs['long_name'] = 'minimum height relative to valley floor'
    dataset['hmin'].attrs['units'] = 'm'

    dataset['interp'].attrs['long_name'] = 'interpolated data point'

    dataset['axis'].attrs['long_name'] = 'stream identifier'
    dataset['swath'].attrs['long_name'] = 'swath identifier'
    dataset['measure'].attrs['long_name'] = 'position along reference axis'
    dataset['measure'].attrs['units'] = 'm'

    dataset.attrs['crs'] = 'EPSG:2154'
    dataset.attrs['FCT'] = 'Fluvial Corridor Toolbox Talweg Height 1.0.5'
    dataset.attrs['Conventions'] = 'CF-1.8'

    # write to netCDF

    dataset.to_netcdf(
        output,
        'w',
        encoding={
            'swath': dict(zlib=True, complevel=9),
            'hmed': dict(zlib=True, complevel=9, least_significant_digit=1),
            'hmin': dict(zlib=True, complevel=9, least_significant_digit=1),
            'interp': dict(zlib=True, complevel=9),
            'measure': dict(zlib=True, complevel=9, least_significant_digit=0)
        })

    return dataset

def ValleyMaskTile(axis, row, col, threshold):

    tileset = config.tileset()

    def _tilename(name):
        return tileset.tilename(name, axis=axis, row=row, col=col)

    datafile = config.filename('metrics_talweg_height', axis=axis)
    hand_raster = _tilename('ax_nearest_height')
    swath_raster = _tilename('ax_valley_swaths')
    output_mask = _tilename('ax_valley_mask_refined')
    # output_height = _tilename('ax_nearest_height_refined')

    if not (os.path.exists(hand_raster) and os.path.exists(swath_raster)):
        return

    data = xr.open_dataset(datafile)

    with rio.open(swath_raster) as ds:
        swaths = ds.read(1)
        swath_nodata = ds.nodata

    with rio.open(hand_raster) as ds:

        hand = ds.read(1)

        nodata = 255
        out = np.full_like(hand, nodata, dtype='uint8')

        for swid in np.unique(swaths):

            if swid == swath_nodata:
                continue

            try:
                talheight = data['hmed'].sel(swath=swid).values
            except KeyError:
                talheight = np.nan

            if np.isnan(talheight):

                swath_mask = (swaths == swid)
                out[swath_mask] = 0

            else:

                # TODO threshold = f(swid, bottom width, drainage area)

                minh = min(-talheight - threshold, -threshold)
                maxh = max(-talheight + threshold, threshold)

                swath_mask = (swaths == swid)
                bottom_mask = (hand >= minh) & (hand < maxh)

                out[swath_mask] = 1
                out[swath_mask & bottom_mask] = 0

        out = features.sieve(out, 100) # TODO externalize parameter
        speedup.reclass_margin(out, 1, 255, 2)

        profile = ds.profile.copy()
        profile.update(dtype='uint8', nodata=nodata, compress='deflate')

        with rio.open(output_mask, 'w', **profile) as dst:
            dst.write(out, 1)

def ValleyMask(axis, threshold, ax_tiles='ax_shortest_tiles', processes=1, **kwargs):
    """
    Refine valley bottom mask
    0: bottom
    1: margin/hole => separate with exterior region algorithm
    """

    tilefile = config.tileset().filename(ax_tiles, axis=axis, **kwargs)

    def length():

        with open(tilefile) as fp:
            return sum(1 for line in fp)

    def arguments():

        with open(tilefile) as fp:
            for line in fp:
                row, col = tuple(int(x) for x in line.split(','))
                yield (
                    ValleyMaskTile,
                    axis,
                    row,
                    col,
                    threshold,
                    kwargs
                )

    with Pool(processes=processes) as pool:

        pooled = pool.imap_unordered(starcall, arguments())

        with click.progressbar(pooled, length=length()) as iterator:
            for _ in iterator:
                pass

def ReclassSwathMargin(axis, row, col, **kwargs):

    tileset = config.tileset()

    def _tilename(name):
        return tileset.tilename(name, axis=axis, row=row, col=col)

    swath_raster = tileset.filename('ax_valley_swaths', axis=axis)
    bottom_raster = tileset.filename('ax_valley_mask_refined', axis=axis)
    swath_raster_tile = _tilename('ax_valley_swaths')
    bottom_raster_tile = _tilename('ax_valley_mask_refined')
    swath_bounds_file = config.filename('ax_valley_swaths_bounds', axis=axis)
    swath_bounds = xr.open_dataset(swath_bounds_file)

    if not os.path.exists(swath_raster_tile):
        return

    with rio.open(swath_raster_tile) as ds:
        swaths = ds.read(1)
        swath_nodata = ds.nodata

    with rio.open(bottom_raster_tile) as ds:
        out = ds.read(1)
        profile = ds.profile.copy()
        height, width = out.shape
        transform = ds.transform

    for swid in np.unique(swaths):

        if swid == swath_nodata:
            continue

        bounds = tuple(swath_bounds['bounds'].sel(label=swid).values)

        with rio.open(swath_raster) as ds:
            window = as_window(bounds, ds.transform)
            swath = ds.read(1, window=window, boundless=True, fill_value=ds.nodata)

        with rio.open(bottom_raster) as ds:
            window = as_window(bounds, ds.transform)
            bottom = ds.read(1, window=window, boundless=True, fill_value=ds.nodata)

        # bottom[(bottom == 1) & (swath != swid)] = 255
        speedup.reclass_margin(bottom, 1, 255, 2)

        # window copy bottom to out

        tile_window = as_window(bounds, transform)

        mini = max(0, tile_window.row_off)
        minj = max(0, tile_window.col_off)
        maxi = min(height - 1, tile_window.row_off + tile_window.height - 1)
        maxj = min(width - 1, tile_window.col_off + tile_window.width - 1)

        wmini = mini - tile_window.row_off
        wminj = minj - tile_window.col_off
        # wmaxi = min(maxi - tile_window.row_off, tile_window.height - 1)
        # wmaxj = min(maxj - tile_window.col_off, tile_window.width - 1)
        wmaxi = maxi - tile_window.row_off
        wmaxj = maxj - tile_window.col_off

        if maxi < 0 or maxj < 0:
            continue

        try:

            assert 0 <= mini <= maxi < height, '%d, %d' % (mini, maxi)
            assert 0 <= minj <= maxj < width, '%d, %d' % (minj, maxj)
            assert 0 <= wmini <= wmaxi < tile_window.height, '%d, %d' % (wmini, wmaxi)
            assert 0 <= wminj <= wmaxj < tile_window.width, '%d, %d' % (wminj, wmaxj)

        except AssertionError as error:

            print(height, width, bottom.shape, tile_window)
            print(mini, minj, maxi, maxj)
            print(wmini, wminj, wmaxi, wmaxj)
            raise error

        out_win = out[mini:maxi+1, minj:maxj+1]
        swath_win = swath[wmini:wmaxi+1, wminj:wmaxj+1]
        bottom_win = bottom[wmini:wmaxi+1, wminj:wmaxj+1]
        mask_win = (swath_win == swid)
        out_win[mask_win] = bottom_win[mask_win]

    with rio.open(bottom_raster_tile + '.tmp', 'w', **profile) as dst:
        dst.write(out, 1)

    return bottom_raster_tile + '.tmp'

def ReclassMargin(axis, processes=1, ax_tiles='ax_shortest_tiles', **kwargs):
    """
    DOCME
    """

    tilefile = config.tileset().filename(ax_tiles, axis=axis, **kwargs)

    def length():

        with open(tilefile) as fp:
            return sum(1 for line in fp)

    def arguments():

        with open(tilefile) as fp:
            for line in fp:
                row, col = tuple(int(x) for x in line.split(','))
                yield (
                    ReclassSwathMargin,
                    axis,
                    row,
                    col,
                    kwargs
                )

    with Pool(processes=processes) as pool:

        pooled = pool.imap_unordered(starcall, arguments())

        tmpfiles = list()

        with click.progressbar(pooled, length=length()) as iterator:
            for tmpfile in iterator:
                if tmpfile is not None:
                    tmpfiles.append(tmpfile)

        for tmpfile in tmpfiles:
            os.rename(tmpfile, tmpfile.replace('.tif.tmp', '.tif'))