import sys

from zucaro.cli import zucaro_cli

MINPYVERSION = (3, 7, 0)

if sys.version_info < MINPYVERSION:
    print(
        "zucaro requires at least Python version "
        "{}.{}.{}. You are using {}.{}.{}.".format(*MINPYVERSION, *sys.version_info)
    )
    sys.exit(1)


def main():
    zucaro_cli()
