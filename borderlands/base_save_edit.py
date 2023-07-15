#!/usr/bin/env python3

import sys
import traceback

from borderlands.bl2 import AppBL2
from borderlands.bltps import AppTPS
from borderlands.utils import python_version_check

ERROR_TEMPLATE = """
Something went wrong, but please ensure you have the latest
version from https://github.com/apocalyptech/borderlands2 before
reporting a bug.  Information useful for a report follows:

Arguments: {}

"""


def run(game_name: str) -> None:
    python_version_check()

    if game_name == 'BL2':
        app_class = AppBL2
    elif game_name == 'TPS':
        app_class = AppTPS
    else:
        raise RuntimeError(f'unknown game: {game_name!r}')

    # noinspection PyBroadException
    try:
        app = app_class(sys.argv[1:])
        app.run()
    except Exception:
        print(ERROR_TEMPLATE.format(repr(sys.argv[1:])), file=sys.stderr)
        traceback.print_exc(None, sys.stderr)
        sys.exit(1)
