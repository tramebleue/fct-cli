# coding: utf-8

"""
Command Line Interface for Corridor Module
"""

import os
import glob
import time
from datetime import datetime
import click

from .. import __version__ as version
from ..config import config
from ..cli import parallel_opt
from ..cli.Decorators import pretty_time_delta
from ..tileio import buildvrt


def PrintCommandInfo(command, axis, processes, parameters):
    """
    Print command info
    """

    start_time = time.time()

    tileset = config.tileset()

    click.secho('Command        : %s' % command, fg='green')
    click.secho('FCT version    : %s' % version)

    click.secho('Tileset        : %s' % tileset.name)
    click.secho('# of tiles     : %d' % len(tileset))

    click.secho('--%16s:' % 'Parameters', fg='cyan')
    click.secho('  %16s: %d' % ('axis', axis))

    for parameter, value in parameters.items():
        click.echo('  %16s: %s' % (parameter, value))

    click.secho('-- Start time      : %s' % datetime.fromtimestamp(start_time))
    click.secho('Running %d processes' % processes, fg='yellow')

    return start_time

@click.group()
def cli():
    """
    Fluvial corridor delineation module
    """

@cli.group()
def height():
    """
    Calculate relative heights from DEM
    """

@height.command()
@click.argument('axis', type=int)
@click.option('--vrt/--no--vrt', default=True, help='Build VRT after processing')
@parallel_opt
def shortest(axis, vrt, processes):
    """
    Relative heights following shortest path to drainage/reference
    """

    # pylint: disable=import-outside-toplevel
    from .ValleyBottomShortest import ValleyBottom, ValleyBottomDefaultParameters

    config.default()
    parameters = ValleyBottomDefaultParameters()
    start_time = PrintCommandInfo('valley bottom shortest', axis, processes, parameters)

    ValleyBottom(axis=axis, processes=processes, **parameters)

    elapsed = time.time() - start_time
    click.secho('Elapsed time   : %s' % pretty_time_delta(elapsed))

    if vrt:

        click.secho('Building output VRTs', fg='cyan')
        buildvrt('default', parameters['dataset_height'], axis=1)
        buildvrt('default', parameters['dataset_distance'], axis=1)