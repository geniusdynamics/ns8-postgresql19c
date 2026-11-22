#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""A tiny stand-in of the NS8 agent package for the unit tests"""

import json
import sys

SD_ERR = "<3>"
SD_WARNING = "<4>"
SD_NOTICE = "<5>"
SD_INFO = "<6>"
SD_DEBUG = "<7>"

STATUS = []
REDIS = None


class ValidationExit(SystemExit):
    pass


def set_status(value):
    STATUS.append(value)


def set_weight(*args):
    pass


def set_progress(*args):
    pass


def redis_connect(**kwargs):
    return REDIS


def list_service_providers(rdb, service, transport, filters=None):
    return rdb.providers(service, transport) if rdb else []


def read_envfile(path):
    values = {}
    with open(path) as fp:
        for line in fp:
            line = line.rstrip("\n")
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k] = v
    return values
