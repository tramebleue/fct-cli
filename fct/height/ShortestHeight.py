# coding: utf-8

"""
Valley bottom extraction procedure - shortest path exploration

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
from collections import namedtuple
import itertools
from operator import itemgetter
from multiprocessing import Pool

import numpy as np
import click

import rasterio as rio
import fiona

from ..config import (
    config,
    LiteralParameter,
    DatasetParameter
)
from ..cli import starcall
from .. import transform as fct
from .. import speedup
from ..tileio import (
    PadRaster,
    border
)

class Parameters:
    """
    Shortesh height parameters
    """

    dem = DatasetParameter('elevation raster (DEM)', type='input')
    reference = DatasetParameter('reference network shapefile', type='input')
    mask = DatasetParameter('height raster defining domain mask', type='input')

    tiles = DatasetParameter('domain tiles as CSV list', type='output')
    height = DatasetParameter('height raster', type='output')
    distance = DatasetParameter('distance to reference pixel', type='output')
    state = DatasetParameter('processing state raster', type='output')

    scale_distance = LiteralParameter(
        'scale distance output with given scale factor, corresponding to pixel resolution')
    mask_height_max = LiteralParameter(
        'maximum height defining domain mask')
    height_max = LiteralParameter(
        'stop at maximum height above reference')
    distance_min = LiteralParameter(
        'minimum distance before applying stop criteria, expressed in pixels)')
    distance_max = LiteralParameter(
        'stop at maximum distance, expressed in pixels')
    jitter = LiteralParameter(
        'apply jitter on performing shortest path exploration')
    tmp_suffix = LiteralParameter(
        'temporary files suffix')

    def __init__(self, axis=None):
        """
        Default parameter values,
        with reference elevation = talweg
        """

        self.dem = 'dem'
        self.mask = 'off' # 'nearest_height'

        if axis is None:

            self.tiles = 'shortest_tiles'
            self.reference = 'network-cartography-ready'
            self.height = 'shortest_height'
            self.distance = 'shortest_distance'
            self.state = 'shortest_state'

        else:

            self.tiles = dict(key='ax_shortest_tiles', axis=axis)
            self.reference = dict(key='ax_talweg', axis=axis)
            self.height = dict(key='ax_shortest_height', axis=axis)
            self.distance = dict(key='ax_shortest_distance', axis=axis)
            self.state = dict(key='ax_shortest_state', axis=axis)

        self.scale_distance = 1.0
        self.mask_height_max = 20.0
        self.height_max = 20.0
        self.distance_min = 20
        self.distance_max = 2000
        self.jitter = 0.4
        self.tmp_suffix = '.tmp'


def ShortestHeightTile(row, col, seeds, params, **kwargs):
    """
    Valley bottom shortest path exploration
    """

    elevations, profile = PadRaster(row, col, params.dem, padding=1, **params.dem.arguments(kwargs))
    transform = profile['transform']
    nodata = profile['nodata']
    height, width = elevations.shape

    # if not params.mask.none:

    #     mask, mask_profile = PadRaster(row, col, params.mask.name, padding=1)
    #     mask_nodata = mask_profile['nodata']
    #     elevations[mask == mask_nodata] = nodata

    #     del mask

    output_height = str(params.height.tilename(row=row, col=col, **kwargs))
    # config.tileset().tilename('shortest_height', row=row, col=col)
    output_distance = str(params.distance.tilename(row=row, col=col, **kwargs))
    # config.tileset().tilename('shortest_distance', row=row, col=col)
    output_state = str(params.state.tilename(row=row, col=col, **kwargs))
    # config.tileset().tilename('shortest_state', row=row, col=col)

    if os.path.exists(output_height):

        heights, _ = PadRaster(row, col, params.height, padding=1, **params.height.arguments(kwargs))
        distance, _ = PadRaster(row, col, params.distance, padding=1, **params.distance.arguments(kwargs))
        state, _ = PadRaster(row, col, params.state, padding=1, **params.state.arguments(kwargs))

    else:

        heights = np.full((height, width), nodata, dtype='float32')
        distance = np.full((height, width), nodata, dtype='float32')
        state = np.zeros((height, width), dtype='uint8')
        state[elevations == nodata] = 255

        if not params.mask.none:

            mask, mask_profile = PadRaster(row, col, params.mask.name, padding=1, **params.mask.arguments(kwargs))
            mask_nodata = mask_profile['nodata']

            if params.mask_height_max > 0:

                height_max = params.mask_height_max
                valid = (mask != mask_nodata) & (mask >= -height_max) & (mask <= height_max)
                state[~valid] = 255
                del valid

            else:

                state[mask == mask_nodata] = 255

            del mask

    coord = itemgetter(0, 1)
    pixels = fct.worldtopixel(np.array([coord(seed) for seed in seeds], dtype='float32'), transform)
    intile = np.array([0 <= i < height and 0 <= j < width for i, j in pixels])

    seed_heights = np.array([seed[2] for seed in seeds], dtype='float32')
    seed_distance = np.array([seed[3] for seed in seeds], dtype='float32')

    pixels = pixels[intile]
    seed_heights = seed_heights[intile]
    seed_distance = seed_distance[intile]

    recorded_distance = distance[pixels[:, 0], pixels[:, 1]]
    shortest = (recorded_distance == nodata) | (seed_distance < recorded_distance)

    pixels = pixels[shortest]
    heights[pixels[:, 0], pixels[:, 1]] = seed_heights[shortest]
    distance[pixels[:, 0], pixels[:, 1]] = seed_distance[shortest]

    state[pixels[:, 0], pixels[:, 1]] = 1
    control = np.copy(state)

    reference = elevations - heights
    del heights

    speedup.valley_bottom_shortest(
        elevations,
        state,
        reference,
        distance,
        max_dz=params.height_max,
        min_distance=params.distance_min,
        max_distance=params.distance_max,
        jitter=params.jitter)

    spillovers = [
        (i, j) for i, j in border(height, width)
        if control[i, j] != 2 and state[i, j] == 2
    ]

    del control

    # restore unresolved cells' state
    state[state == 1] = 2

    heights = elevations - reference
    heights[(state == 0) | (state == 255)] = nodata
    distance[(state == 0) | (state == 255)] = nodata

    if spillovers:

        xy = fct.pixeltoworld(np.array(spillovers, dtype='int32'), transform)

        def attributes(k, ij):
            """
            Returns (row, col, x, y, height, distance)
            """

            i, j = ij

            if i == 0:
                prow = row - 1
            elif i == height-1:
                prow = row + 1
            else:
                prow = row

            if j == 0:
                pcol = col - 1
            elif j == width-1:
                pcol = col + 1
            else:
                pcol = col

            return (prow, pcol) + tuple(xy[k]) + (heights[i, j], distance[i, j])

        spillovers = [
            attributes(k, ij)
            for k, ij in enumerate(spillovers)
        ]

    del elevations
    del reference

    heights = heights[1:-1, 1:-1]
    distance = distance[1:-1, 1:-1]
    state = state[1:-1, 1:-1]

    height, width = heights.shape
    transform = transform * transform.translation(1, 1)
    profile.update(
        dtype='float32',
        height=height,
        width=width,
        transform=transform,
        compress='deflate')

    output_height += params.tmp_suffix
    output_distance += params.tmp_suffix
    output_state += params.tmp_suffix

    with rio.open(output_height, 'w', **profile) as dst:
        dst.write(heights, 1)

    with rio.open(output_distance, 'w', **profile) as dst:
        dst.write(distance, 1)

    profile.update(dtype='uint8', nodata=255)

    with rio.open(output_state, 'w', **profile) as dst:
        dst.write(state, 1)

    return spillovers, (output_height, output_distance, output_state)

def ShortestHeightIteration(params, spillovers, ntiles, processes=1, **kwargs):
    """
    Multiprocessing wrapper for ValleyBottomTile
    """

    tile = itemgetter(0, 1)
    coordxy = itemgetter(2, 3)
    values = itemgetter(4, 5)

    spillovers = sorted(spillovers, key=tile)
    tiles = itertools.groupby(spillovers, key=tile)

    g_spillover = list()
    tmpfiles = list()

    if processes == 1:

        for (row, col), seeds in tiles:
            seeds = [coordxy(seed) + values(seed) for seed in seeds]
            t_spillover, tmps = ShortestHeightTile(row, col, seeds, params, **kwargs)
            g_spillover.extend(t_spillover)
            tmpfiles.extend(tmps)

        for tmpfile in tmpfiles:
            os.rename(tmpfile, tmpfile.replace('.tif' + params.tmp_suffix, '.tif'))

    else:

        def arguments():

            for (row, col), seeds in tiles:

                seeds = [coordxy(seed) + values(seed) for seed in seeds]
                yield (ShortestHeightTile, row, col, seeds, params, kwargs)

        with Pool(processes=processes) as pool:

            pooled = pool.imap_unordered(starcall, arguments())
            with click.progressbar(pooled, length=ntiles) as iterator:
                for t_spillover, tmps in iterator:
                    g_spillover.extend(t_spillover)
                    tmpfiles.extend(tmps)

            # with click.progressbar(pooled, length=len(arguments)) as bar:
            #     for t_spillover in bar:
            #         g_spillover.extend(t_spillover)

        for tmpfile in tmpfiles:
            os.rename(tmpfile, tmpfile.replace('.tif' + params.tmp_suffix, '.tif'))

    return g_spillover

def ScaleShortestDistanceTile(row, col, params, **kwargs):

    distance_raster = params.distance.tilename(row=row, col=col, **kwargs)

    with rio.open(distance_raster) as ds:

        distance = params.scale_distance * ds.read(1)
        profile = ds.profile.copy()

    profile.update(compress='deflate')

    with rio.open(distance_raster, 'w', **profile) as dst:

        dst.write(distance, 1)

def ShortestHeight(params, processes=1, **kwargs):
    """
    Valley bottom extraction procedure - shortest path exploration
    """

    def generate_seeds(feature):

        for point in feature['geometry']['coordinates']:
            x, y = point[:2]
            row, col = config.tileset().index(x, y)
            yield (row, col, x, y, 0.0, 0.0)

    # network_shapefile = config.filename('ax_talweg', axis=axis)
    network_shapefile = params.reference.filename(tileset=None)
    # config.filename('network-cartography-ready')

    with fiona.open(network_shapefile) as fs:

        seeds = [
            seed
            for feature in fs
            for seed in generate_seeds(feature)
        ]

    count = 0
    tile = itemgetter(0, 1)
    g_tiles = set()

    while seeds:

        count += 1
        tiles = {tile(s) for s in seeds}
        g_tiles.update(tiles)
        click.echo('Iteration %02d -- %d spillovers, %d tiles' % (count, len(seeds), len(tiles)))

        seeds = ShortestHeightIteration(params, seeds, len(tiles), processes, **kwargs)

    tiles = {tile(s) for s in seeds}
    g_tiles.update(tiles)

    click.secho('Ok', fg='green')

    output = params.tiles.filename()
    # config.tileset().filename('shortest_tiles')

    with open(output, 'w') as fp:
        for row, col in sorted(g_tiles):
            fp.write('%d,%d\n' % (row, col))

    if params.scale_distance != 1.0:

        click.echo('Scaling distance output ...')

        def arguments():

            for row, col in g_tiles:
                yield (
                    ScaleShortestDistanceTile,
                    row,
                    col,
                    params,
                    kwargs
                )

        with Pool(processes=processes) as pool:

            pooled = pool.imap_unordered(starcall, arguments())
            with click.progressbar(pooled, length=len(g_tiles)) as iterator:
                for _ in iterator:
                    pass
