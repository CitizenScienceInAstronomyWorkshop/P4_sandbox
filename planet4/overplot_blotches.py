from __future__ import print_function, division
from .io import get_image_from_record
import os
import pandas as pd
import matplotlib.pyplot as plt
from itertools import cycle
from matplotlib.patches import Ellipse
import sys

data_root = '/Users/maye/data/planet4'


def data_munging(_id):
    print("Reading current marked data.")
    data = pd.read_hdf(os.path.join(data_root, 'marked.h5'), 'df',
                       where='image_id=='+_id)
    print("Done.")
    return data


def get_blotches(data, _id):
    """get the blotches for given image_id"""
    subframe = data[data.image_id == _id]
    blotches = subframe[subframe.marking == 'blotch']
    return blotches


def get_fans(data, _id):
    """get the fans for given image_id"""
    subframe = data[data.image_id == _id]
    fans = subframe[subframe.marking == 'fan']
    return fans


def get_image_name_from_data(data):
    """This assumes that all data is for the same image!"""
    line = data.iloc[0]
    return os.path.basename(line.image_url.strip())


def add_ellipses_to_axis(ax, blotches):
    colors = cycle('bgrcmyk')
    for i, color in zip(range(len(blotches),), colors):
        line = blotches.iloc[i]
        ax.scatter(line.x, line.y, color=color)
        el = Ellipse((line.x, line.y),
                     line.radius_1, line.radius_2, line.angle,
                     fill=False, color=color, linewidth=1)
        ax.add_artist(el)


def plot_blotches(data, _id):
    blotches = get_blotches(data, _id)
    # this will endlessly cycle through the colors given
    _, ax = plt.subplots(ncols=2)
    img = get_image_from_record(blotches.iloc[0])
    ax[0].imshow(img)
    im = ax[1].imshow(img)
    add_ellipses_to_axis(ax[1], blotches)
    ax[0].set_title('image_id {}'.format(_id))
    plt.savefig('plots/blotches_'+get_image_name_from_data(blotches),
                bbbox_inches='tight')
    plt.show()
    return im


if __name__ == '__main__':
    # img id should be given on command line
    img_id = sys.argv[1]
    mdata = data_munging(img_id)
    plot_blotches(mdata, img_id)
