# coding: utf-8

"""
Configuration Module

***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*                                                                         *
***************************************************************************
"""

from .Configuration import Configuration
config = Configuration()

from .descriptors import (
    LiteralParameter,
    DatasourceParameter,
    DatasetParameter,
    WorkflowContext,
    FileResource
)
