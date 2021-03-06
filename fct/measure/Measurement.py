# coding: utf-8

"""
Longitudinal swath generation :
discretize space along reference axis

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
import math
from operator import itemgetter
from collections import namedtuple
from multiprocessing import Pool

import numpy as np
from scipy.spatial import cKDTree

import click
import xarray as xr

import rasterio as rio
from rasterio import features
import fiona
import fiona.crs
from shapely.geometry import (
    asShape,
    Polygon,
    LineString)

from ..config import (
    config,
    LiteralParameter,
    DatasetParameter
)
# from ..tileio import ReadRasterTile
from ..tileio import as_window
from ..rasterize import rasterize_linestring, rasterize_linestringz
from .. import transform as fct
from .. import speedup
from .. import terrain_analysis as ta
from ..cli import starcall
from ..metadata import set_metadata

def nearest_value_and_distance(refpixels, domain, nodata):
    """
    Returns distance in pixels !
    """

    height, width = domain.shape
    midpoints = 0.5*(refpixels + np.roll(refpixels, 1, axis=0))
    midpoints_valid = (refpixels[:-1, 3] == refpixels[1:, 3])
    # midpoints = midpoints[1:, :2][midpoints_valid]
    midpoints = midpoints[1:, :2]
    midpoints[~midpoints_valid, 0] = -999999.0
    midpoints[~midpoints_valid, 1] = -999999.0

    midpoints_index = cKDTree(midpoints, balanced_tree=True)
    distance = np.zeros_like(domain)
    values = np.copy(distance)
    nearest_axes = np.zeros_like(domain, dtype='uint32')

    # semi-vectorized code, easier to understand

    # for i in range(height):

    #     js = np.arange(width)
    #     row = np.column_stack([np.full_like(js, i), js])
    #     valid = domain[row[:, 0], row[:, 1]] != nodata
    #     query_pixels = row[valid]
    #     nearest_dist, nearest_idx = midpoints_index.query(query_pixels, k=1, jobs=4)
    #     nearest_a = np.take(refpixels, nearest_idx, axis=0, mode='wrap')
    #     nearest_b = np.take(refpixels, nearest_idx+1, axis=0, mode='wrap')
    #     nearest_m = np.take(midpoints[:, 2], nearest_idx+1, axis=0, mode='wrap')
    #     # same as
    #     # nearest_value = 0.5*(nearest_a[:, 2] + nearest_b[:, 2])
    #     dist, signed_dist, pos = ta.signed_distance(
    #         np.float32(nearest_a),
    #         np.float32(nearest_b),
    #         np.float32(query_pixels))

    # faster fully-vectorized code

    pixi, pixj = np.meshgrid(np.arange(height), np.arange(width), indexing='ij')
    valid = domain != nodata
    query_pixels = np.column_stack([pixi[valid], pixj[valid]])

    del pixi
    del pixj
    del valid

    _, nearest_idx = midpoints_index.query(query_pixels, k=1)

    # nearest_a = np.take(refpixels, nearest_idx, axis=0, mode='wrap')
    # nearest_b = np.take(refpixels, nearest_idx+1, axis=0, mode='wrap')

    nearest_p = np.take(refpixels, np.column_stack([nearest_idx, nearest_idx+1]), axis=0, mode='wrap')
    nearest_a = nearest_p[:, 0, :]
    nearest_b = nearest_p[:, 1, :]

    dist, signed_dist, pos = ta.signed_distance(
        np.float32(nearest_a),
        np.float32(nearest_b),
        np.float32(query_pixels))

    # interpolate between points A and B
    nearest_value = nearest_a[:, 2] + pos*(nearest_b[:, 2] - nearest_a[:, 2])

    nearest_axis = np.copy(nearest_a[:, 3])
    nearest_axis[nearest_axis != nearest_b[:, 3]] = 0

    # almost same as
    # nearest_m = 0.5*(nearest_a[:, 2] + nearest_b[:, 2])
    # same as
    # nearest_m = np.take(midpoints[:, 2], nearest_idx+1, axis=0, mode='wrap')

    distance[query_pixels[:, 0], query_pixels[:, 1]] = dist * np.sign(signed_dist)
    values[query_pixels[:, 0], query_pixels[:, 1]] = nearest_value
    nearest_axes[query_pixels[:, 0], query_pixels[:, 1]] = nearest_axis

    return nearest_axes, values, distance

class Parameters:
    """
    Swath measurement parameters
    """

    tiles = DatasetParameter(
        'domain tiles as CSV list',
        type='input')
    mask = DatasetParameter(
        'height raster defining domain mask',
        type='input')
    reference = DatasetParameter(
        'reference axis shapefile (eg. valley medial axis)',
        type='input')
    talweg_distance = DatasetParameter(
        'distance to reference pixels (raster)',
        type='input')
    output_distance = DatasetParameter(
        'distance to nearest reference axis (raster)',
        type='output')
    output_measure = DatasetParameter(
        'location along nearest reference axis (raster)',
        type='output')
    output_nearest = DatasetParameter(
        'nearest reference axis (raster)',
        type='output')

    # ref_points_distance = LiteralParameter(
    #     'distance between generated points along reference axis')

    def __init__(self):
        """
        Default parameter values
        """

        self.tiles = 'shortest_tiles'
        self.mask = 'nearest_height'
        self.reference = 'refaxis'
        self.talweg_distance = 'nearest_distance'
        self.output_distance = 'axis_distance'
        self.output_measure = 'axis_measure'
        self.output_nearest = 'axis_nearest'
        # self.ref_points_distance = 10.0

def MeasureTile(row, col, params, **kwargs):
    """
    see CarveLongitudinalSwaths
    """

    refaxis_shapefile = params.reference.filename(tileset=None)
    # config.filename(params.ax_reference)
    mask_raster = params.mask.tilename(row=row, col=col)
    # _tilename(params.ax_mask)

    output_distance = params.output_distance.tilename(row=row, col=col)
    # _tilename(params.output_distance)
    output_measure = params.output_measure.tilename(row=row, col=col)
    # _tilename(params.output_measure)
    output_nearest = params.output_nearest.tilename(row=row, col=col)
    # _tilename(params.output_nearest)

    if not os.path.exists(mask_raster):
        return {}

    with rio.open(mask_raster) as ds:

        # click.echo('Read Valley Bottom')

        # valley_bottom = speedup.raster_buffer(ds.read(1), ds.nodata, 6.0)
        mask = ds.read(1)
        height, width = mask.shape

        # distance = np.full_like(valley_bottom, ds.nodata)
        # measure = np.copy(distance)
        refaxis_pixels = list()

        # click.echo('Map Stream Network')

        def accept(i, j):
            return all([i >= -height, i < 2*height, j >= -width, j < 2*width])

        coord = itemgetter(0, 1)

        mmin = float('inf')
        mmax = float('-inf')

        with fiona.open(refaxis_shapefile) as fs:
            for feature in fs:

                axis = feature['properties']['AXIS']
                m0 = feature['properties'].get('M0', 0.0)
                geometry = asShape(feature['geometry'])
                length = geometry.length

                if m0 < mmin:
                    mmin = m0

                if m0 + length > mmax:
                    mmax = m0 + length

                # if params.ref_points_distance > 0:

                #     measures = np.arange(0, length, params.ref_points_distance)
                #     coordinates = np.array([
                #         geometry.interpolate(x).coords[0]
                #         for x in measures
                #     ], dtype='float32')

                #     coordinates = np.column_stack([
                #         fct.worldtopixel(coordinates, ds.transform),
                #         m0 + length - measures
                #     ])

                # else:

                coordinates = np.array([
                    coord(p) + (m0,) for p in reversed(feature['geometry']['coordinates'])
                ], dtype='float32')

                coordinates[1:, 2] = m0 + np.cumsum(np.linalg.norm(
                    coordinates[1:, :2] - coordinates[:-1, :2],
                    axis=1))

                coordinates[:, :2] = fct.worldtopixel(coordinates[:, :2], ds.transform)

                for a, b in zip(coordinates[:-1], coordinates[1:]):
                    for i, j, m in rasterize_linestringz(a, b):
                        if accept(i, j):
                            # distance[i, j] = 0
                            # measure[i, j] = m
                            refaxis_pixels.append((i, j, m, axis))

        # ta.shortest_distance(axr, ds.nodata, startval=1, distance=distance, feedback=ta.ConsoleFeedback())
        # ta.shortest_ref(axr, ds.nodata, startval=1, fillval=0, out=measure, feedback=ta.ConsoleFeedback())

        if not refaxis_pixels:
            return []

        # click.echo('Calculate Measure & Distance Raster')

        # Option 1, shortest distance

        # speedup.shortest_value(valley_bottom, measure, ds.nodata, distance, 1000.0)
        # distance = 5.0 * distance
        # distance[valley_bottom == ds.nodata] = ds.nodata
        # measure[valley_bottom == ds.nodata] = ds.nodata
        # Add 5.0 m x 10 pixels = 50.0 m buffer
        # distance2 = np.zeros_like(valley_bottom)
        # speedup.shortest_value(domain, measure, ds.nodata, distance2, 10.0)

        # Option 2, nearest using KD Tree

        nearest, measure, distance = nearest_value_and_distance(
            np.flip(np.array(refaxis_pixels), axis=0),
            np.float32(mask),
            ds.nodata)

        nodata = -99999.0
        distance = 5.0 * distance
        distance[mask == ds.nodata] = nodata
        measure[mask == ds.nodata] = nodata

        # click.echo('Write output')

        profile = ds.profile.copy()
        profile.update(compress='deflate', dtype='float32', nodata=nodata)

        with rio.open(output_distance, 'w', **profile) as dst:
            dst.write(distance, 1)

        with rio.open(output_measure, 'w', **profile) as dst:
            dst.write(measure, 1)

        profile.update(dtype='uint32', nodata=0)

        with rio.open(output_nearest, 'w', **profile) as dst:
            dst.write(nearest, 1)

def MeasureNetwork(params, processes=1, **kwargs):
    """
    Calculate measurement support rasters and
    create discrete longitudinal swath units along the reference axis
    """

    tilefile = params.tiles.filename() # config.tileset().filename(ax_tiles)

    def length():

        with open(tilefile) as fp:
            return sum(1 for line in fp)

    def arguments():

        with open(tilefile) as fp:
            tiles = [tuple(int(x) for x in line.split(',')) for line in fp]

        for row, col in tiles:
            yield (
                MeasureTile,
                row,
                col,
                params,
                kwargs
            )

    with Pool(processes=processes) as pool:

        pooled = pool.imap_unordered(starcall, arguments())

        with click.progressbar(pooled, length=length()) as iterator:
            for _ in iterator:
                pass
