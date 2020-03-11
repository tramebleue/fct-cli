# distutils: language = c++
# cython: language_level=3, c_string_type=str, c_string_encoding=ascii, embedsignature=True

"""
DOCME

***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*                                                                         *
***************************************************************************
"""

import cython
import click
cimport cython

import numpy as np
cimport numpy

import array
from cpython cimport array

from libcpp.deque cimport deque
from libcpp.map cimport map
from libcpp.pair cimport pair
from libcpp.queue cimport priority_queue
from libcpp.stack cimport stack
from libcpp.vector cimport vector
from cython.operator cimport dereference, preincrement

cdef extern from "<algorithm>" namespace "std":
    cdef T min[T](T a, T b) nogil
    cdef T max[T](T a, T b) nogil

ctypedef short D8Flow

ctypedef float ContributingArea
ctypedef pair[int, int] Cell
ctypedef stack[Cell] CellStack
ctypedef deque[Cell] CellQueue
ctypedef pair[float, Cell] ZCell
ctypedef priority_queue[ZCell] ZPriorityQueue

ctypedef unsigned int Label
ctypedef pair[Label, Label] LabelPair
ctypedef pair[long, long] Pixel
ctypedef pair[Pixel, long] ContributingPixel
ctypedef map[Pixel, ContributingPixel] Graph
ctypedef pair[Pixel, ContributingPixel]  GraphItem
ctypedef map[Pixel, ContributingPixel].iterator GraphIterator
ctypedef map[Pixel, int] Degree
ctypedef map[Pixel, float] CumAreas
ctypedef map[Pixel, bint] PixTracker
ctypedef deque[Pixel] PixQueue
# ctypedef deque[Pixel].iterator PixQueueIterator

#                                    0   1   2   3   4   5   6   7
#                                    N  NE   E  SE   S  SW   W  NW
cdef int[8] ci = array.array('i', [ -1, -1,  0,  1,  1,  1,  0, -1 ])
cdef int[8] cj = array.array('i', [  0,  1,  1,  1,  0, -1, -1, -1 ])


# upward = np.power(2, np.array([ 4,  5,  6,  7,  0,  1,  2,  3 ], dtype=np.uint8))
cdef unsigned char[8] upward = array.array('B', [ 16,  32,  64,  128,  1,  2,  4,  8 ])

cdef inline bint ingrid(long height, long width, long i, long j) nogil:

    return (i >= 0) and (i < height) and (j >= 0) and (j < width)

cdef inline int ilog2(unsigned char x) nogil:

    cdef int r = 0

    if x == 0:
        return -1

    while x != 1:
        r += 1
        x = x >> 1

    return r

include "Flats.pxi"
include "FlowAccumulation.pxi"
include "GraphAcc.pxi"
include "Streams.pxi"