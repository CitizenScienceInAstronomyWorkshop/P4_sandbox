"""This script requires to launch a local ipcontroller. If you execute this
locally, do it with `ipcluster start`.
"""
import argparse
import glob
import logging
import os
import sys
import time

from ipyparallel import Client
from ipyparallel.util import interactive

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)


@interactive
def process_fname(fname):
    import pandas as pd
    newfname = fname[:-3] + 'csv'
    pd.read_hdf(fname, 'df').to_csv(newfname)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory',
                        help="Provide the directory of the HDF files "
                             "that shall be converted to csv here.")
    args = parser.parse_args()

    root = os.path.abspath(args.directory)
    fnames = glob.glob(os.path.join(root, '*.hdf'))
    logging.info('Found %i files to convert.', len(fnames))

    c = Client()
    lbview = c.load_balanced_view()

    results = lbview.map_async(process_fname, fnames)
    # progress display
    while not results.ready():
        print("{:.1f} %".format(100 * results.progress / len(fnames)))
        sys.stdout.flush()
        time.sleep(10)
    logging.info('Conversion done.')
