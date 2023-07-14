import sys

MIN_PYTHON = (3, 9)


def version_check():
    global MIN_PYTHON
    if sys.version_info < MIN_PYTHON:
        sys.exit(f'ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} is required to run this utility')
