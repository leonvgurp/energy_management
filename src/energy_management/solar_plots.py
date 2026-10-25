"""Regenerate solar plots from saved output files, without source data or network."""

import argparse
from pathlib import Path

from .solar import plot_saved_outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    plot_saved_outputs(args.input)


if __name__ == "__main__":
    main()
