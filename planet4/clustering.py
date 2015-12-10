from __future__ import division, print_function

import logging

import importlib
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import display
from ipywidgets import FloatText
from pathlib import Path

from . import io, markings, plotting
from .dbscan import DBScanner

importlib.reload(logging)
logpath = Path.home() / 'p4reduction.log'
logging.basicConfig(filename=str(logpath), filemode='w', level=logging.DEBUG,
                    format='%(asctime)s %(levelname)s: %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

matplotlib.style.use('bmh')


class ClusteringManager(object):

    """Control class to manage the clustering pipeline.

    Parameters
    ----------
    dbname : str or pathlib.Path, optional
        Path to the database used by DBManager. If not provided, DBManager
        will find the most recent one and use that.
    scope : {'hirise', 'planet4'}
        Switch to control in what coordinates the clustering is happening.
        'hirise' is required to automatically take care of tile overlaps, while
        'planet4' is required to be able to check the quality of single
        Planet4 tiles.
    min_distance : int
        Parameter to control the distance below which fans and blotches are
        determined to be 2 markings of the same thing, creating a FNOTCH
        chimera. The ratio between blotch and fan marks determines a fnotch
        value that can be used as a discriminator for the final object catalog.
    eps : int
        Parameter to control the exclusion distance for the DBSCAN
    fnotched_dir : str or pathlib.Path
        Path to folder where to store output. Default: io.data_root / 'output'
    output_format : {'hdf', 'csv', 'both'}
        Format to save the output in. Default: 'hdf'
    cut : float
        Value to apply for fnotch cutting.

    Attributes
    ----------
    confusion : list
        List of confusion data.
    fnotched_fans : list
        List of Fan objects after fnotches have been removed.
    fnotched_blotches : list
        List of Blotch objects after fnotches have been removed.
    fnotches : list
        List of Fnotch objects, as determined by `do_the_fnotch`.
    clustered_fans : list
        List of clustered Fan objects after clustering and averaging.
    clustered_blotches : list
        List of clustered Blotch objects after clustering and averaging.
    n_clustered_fans
    n_clustered_blotches
    output_dir_clustered : pathlib.Path
        Path to full clustering results, without removal of fnotched clusters.
    cut_dir : pathlib.Path
        Path to final fan and blotch clusters, after applying `cut`.
    """

    def __init__(self, dbname=None, scope='hirise', min_distance=10, eps=10,
                 fnotched_dir=None, output_format='hdf', cut=0.5):
        self.db = io.DBManager(dbname)
        self.dbname = dbname
        self.scope = scope
        self.min_distance = min_distance
        self.eps = eps
        self.cut = cut
        self.confusion = []
        self.output_format = output_format

        self.pm = io.PathManager(fnotched_dir)
        self.pm.setup_folders()

    def __getattr__(self, name):
        return getattr(self.pm, name)

    @property
    def n_clustered_fans(self):
        "int : Number of clustered fans."
        return len(self.clustered_data['fan'])

    @property
    def n_clustered_blotches(self):
        "int : Number of clustered blotches."
        return len(self.clustered_data['blotch'])

    def prepare_DBSCAN_input(self, data, kind):
        # filter for the marking for `kind`
        markings = data[data.marking == kind]
        if len(markings) == 0:
            return None

        if self.scope == 'planet4':
            coords = ['x', 'y']
        elif self.scope == 'hirise':
            coords = ['image_x', 'image_y']

        # Determine the clustering input matrix
        current_X = markings[coords].values
        self.current_markings = markings
        return current_X

    def post_processing(self, dbscanner, kind):
        """Create mean objects out of cluster label members.

        Note: I take the image_id of the marking of the first member of
        the cluster as image_id for the whole cluster. In rare circumstances,
        this could be wrong for clusters in the overlap region.

        Stores output in self.reduced_data dictionary

        Parameters
        ----------
        dbscanner : DBScanner
            DBScanner object
        kind : {'fan', 'blotch'}
            current kind of marking to post-process.

        """
        if kind == 'fan':
            cols = markings.Fan.to_average
            Marking = markings.Fan
        elif kind == 'blotch':
            cols = markings.Blotch.to_average
            Marking = markings.Blotch

        reduced_data = []
        data = self.current_markings
        for cluster_members in dbscanner.reduced_data:
            clusterdata = data[cols].iloc[cluster_members]
            meandata = clusterdata.mean()
            cluster = Marking(meandata)
            # storing n_members into the object for later.
            cluster.n_members = len(cluster_members)
            # storing this saved marker for later in ClusteringManager
            cluster.saved = False
            # store the image_id from first cluster member for whole cluster
            try:
                image_id = data['image_id'].iloc[cluster_members][0]
            # inelegant fudge to account for Categories not having iloc.
            except KeyError:
                image_id = data['image_id'].iloc[cluster_members].values[0]
            cluster.image_id = image_id

            reduced_data.append(cluster)

        self.reduced_data[kind] = reduced_data
        logging.debug("Reduced data to {} {}(e)s.".format(len(reduced_data),
                                                          kind))

    def cluster_data(self, data):
        """Basic clustering.

        For each fan and blotch markings in `data` a DBScanner object is
        created that executes the actual clustering. Depending on `scope`, this
        could be over marking data for one image_id only, or for all data for
        one HiRISE image_name.

        Parameters
        ----------
        data : pandas.DataFrame
            containing both fan and blotch data to be clustered.
        """
        logging.debug('ClusterManager: cluster_data()')
        # reset stored clustered data
        self.reduced_data = {}
        for kind in ['fan', 'blotch']:
            current_X = self.prepare_DBSCAN_input(data, kind)
            dbscanner = DBScanner(current_X, eps=self.eps)
            # storing of clustered data happens in here:
            self.post_processing(dbscanner, kind)
            self.confusion.append((self.data_id, kind,
                                   len(self.current_markings),
                                   len(self.reduced_data[kind]),
                                   dbscanner.n_rejected))

    def do_the_fnotch(self):
        """Combine fans and blotches if necessary.

        Use `min_distance` as criterion for linear algebraic distance between
        average cluster markings to determine if they belong to a Fnotch, a
        chimera object of indecision between a Fan and a Blotch, to be decided
        later in the process by applying a `cut` on the resulting Fnotch
        objects.

        See Also
        --------
        markings.Fnotch : The Fnotch object with a `get_marking` method for a
            `cut` value.
        """

        logging.debug("CM: do_the_fnotch")
        from numpy.linalg import norm
        n_close = 0
        fnotches = []
        blotches = []
        fans = []
        for blotch in self.reduced_data['blotch']:
            for fan in self.reduced_data['fan']:
                delta = blotch.center - fan.midpoint
                if norm(delta) < self.min_distance:
                    fnotch_value = calc_fnotch(fan.n_members, blotch.n_members)
                    fnotch = markings.Fnotch(fnotch_value, fan, blotch)
                    fnotch.n_fan_members = fan.n_members
                    fnotch.n_blotch_members = blotch.n_members
                    fnotches.append(fnotch)
                    n_close += 1
                    blotch.saved = True
                    fan.saved = True
            # only after going through all fans for this one blotch, I can store it as an
            # unfnotched blotch:
            if not blotch.saved:
                blotches.append(blotch)
        # I have to wait until the loop over blotches is over, before I know that a fan really
        # never was matched with a blotch, before I store it as an unfnotched Fan.
        for fan in self.reduced_data['fan']:
            if not fan.saved:
                fans.append(fan)
                fan.saved = True

        self.fnotches = fnotches
        self.fnotched_blotches = blotches
        self.fnotched_fans = fans

    def execute_pipeline(self, data):
        """Execute the standard list of methods for catalog production.

        Parameters
        ----------
        data : pandas.DataFrame
            The dataframe containing the data to be clustered.
        """
        self.cluster_data(data)
        self.do_the_fnotch()
        logging.debug("Clustering and fnotching completed.")
        self.store_output()
        self.apply_fnotch_cut()

    def cluster_image_id(self, image_id, data=None):
        """Process the clustering for one image_id.

        Parameters
        ----------
        image_id : str
            Planetfour `image_id`
        """
        logging.info("Clustering data for {}".format(image_id))
        self.data_id = image_id
        if data is None:
            self.p4id = markings.ImageID(image_id, self.dbname)
            data = self.p4id.data
        self.execute_pipeline(data)

    def cluster_image_name(self, image_name, data=None):
        """Process the clustering and fnoching pipeline for a HiRISE image_name."""
        logging.info("Clustering data for {}".format(image_name))
        if data is None:
            data = self.db.get_image_name_markings(image_name)
        self.data_id = image_name
        self.execute_pipeline(data)

    def store_output(self):
        "Write out the clustered and fnotched data."

        logging.debug('CM: Writing output files.')
        logging.debug('CM: Output dir: {}'.format(self.fnotched_dir))
        outfnotch = self.data_id + '_fnotches'
        outblotch = self.data_id + '_blotches'
        outfan = self.data_id + '_fans'
        outdir = self.fnotched_dir
        outdir.mkdir(exist_ok=True)
        # first write the fnotched data
        for outfname, outdata in zip([outfnotch, outblotch, outfan],
                                     [self.fnotches, self.fnotched_blotches,
                                      self.fnotched_fans]):
            if len(outdata) == 0:
                continue
            outpath = outdir / outfname
            series = [cluster.store() for cluster in outdata]
            df = pd.DataFrame(series)
            self.save(df, outpath)
        # store the unfnotched data as well:
        outdir = self.output_dir_clustered
        outdir.mkdir(exist_ok=True)
        for outfname, outdata in zip([outblotch, outfan],
                                     [self.reduced_data['blotch'],
                                      self.reduced_data['fan']]):
            if len(outdata) == 0:
                continue
            outpath = outdir / outfname
            series = [cluster.store() for cluster in outdata]
            df = pd.DataFrame(series)
            self.save(df, outpath)

    def cluster_all(self):
        image_names = self.db.image_names
        ft = FloatText()
        display(ft)
        for i, image_name in enumerate(image_names):
            perc = 100 * i / len(image_names)
            # print('{:.1f}'.format())
            ft.value = round(perc, 1)
            self.cluster_image_name(image_name)

    def report(self):
        print("Fnotches:", len(self.fnotches))
        print("Fans:", len(self.fnotched_fans))
        print("Blotches:", len(self.fnotched_blotches))

    @property
    def confusion_data(self):
        return pd.DataFrame(self.confusion, columns=['image_name', 'kind',
                                                     'n_markings',
                                                     'n_cluster_members',
                                                     'n_rejected'])

    def save_confusion_data(self, fname):
        self.confusion_data.to_csv(fname)

    def get_newfans_newblotches(self):
        df = self.pm.fnotchdf

        # apply Fnotch method `get_marking` with given cut.
        final_clusters = df.apply(markings.Fnotch.from_series, axis=1).\
            apply(lambda x: x.get_marking(self.cut))

        def filter_for_fans(x):
            if isinstance(x, markings.Fan):
                return x

        def filter_for_blotches(x):
            if isinstance(x, markings.Blotch):
                return x

        # now need to filter for whatever object was returned by Fnotch.get_marking
        self.newfans = final_clusters[
            final_clusters.apply(filter_for_fans).notnull()]
        self.newblotches = final_clusters[
            final_clusters.apply(filter_for_blotches).notnull()]

    def save(self, obj, path):
        obj.to_hdf(str(path.with_suffix('.hdf')), 'df')
        obj.to_csv(str(path.with_suffix('.csv')))

    def apply_fnotch_cut(self, cut=None):
        if cut is None:
            cut = self.cut
        # storage path for the final catalog after applying `cut`
        cut_dir = self.fnotched_dir / 'applied_cut_{:.1f}'.format(cut)
        cut_dir.mkdir(exist_ok=True)
        self.cut_dir = cut_dir

        self.pm.id_ = self.data_id

        self.get_newfans_newblotches()

        if len(self.newfans) > 0:
            newfans = self.newfans.apply(lambda x: x.store())
            try:
                completefans = pd.DataFrame(
                    self.pm.fandf()).append(newfans, ignore_index=True)
            except OSError:
                completefans = newfans
        else:
            completefans = self.pm.fandf()
        if len(self.newblotches) > 0:
            newblotches = self.newblotches.apply(lambda x: x.store())
            try:
                completeblotches = pd.DataFrame(
                    self.pm.blotchdf()).append(newblotches, ignore_index=True)
            except OSError:
                completeblotches = newblotches
        else:
            completeblotches = self.pm.blotchdf()
        self.finalfanfname = cut_dir / self.pm.fanfile().name
        self.finalblotchfname = cut_dir / self.pm.blotchfile().name
        self.save(completefans, self.finalfanfname)
        self.save(completeblotches, self.finalblotchfname)


def get_mean_position(fan, blotch, scope):
    if scope == 'hirise':
        columns = ['hirise_x', 'hirise_y']
    else:
        columns = ['x', 'y']

    df = pd.DataFrame([fan.data[columns], blotch.data[columns]])
    return df.mean()


def calc_fnotch(nfans, nblotches):
    return (nfans)/(nfans+nblotches)


def gold_star_plotter(gold_id, axis, blotches=True, kind='blotches'):
    for goldstar, color in zip(markings.gold_members,
                               markings.gold_plot_colors):
        if blotches:
            gold_id.plot_blotches(user_name=goldstar, ax=axis,
                                  user_color=color)
        if kind == 'fans':
            gold_id.plot_fans(user_name=goldstar, ax=axis, user_color=color)
        markings.gold_legend(axis)


def is_catalog_production_good():
    from pandas.core.index import InvalidIndexError
    db = DBManager(get_current_database_fname())
    not_there = []
    invalid_index = []
    value_error = []
    for image_name in db.image_names:
        try:
            ResultManager(image_name)
        except InvalidIndexError:
            invalid_index.append(image_name)
        except ValueError:
            value_error.append(image_name)
        except:
            not_there.append(image_name)
    if len(value_error) == 0 and len(not_there) == 0 and\
            len(invalid_index) == 0:
        return True
    else:
        return False


def main():
    gold_ids = io.common_gold_ids()

    p4img = markings.ImageID(gold_ids[10])
    golddata = p4img.data[p4img.data.user_name.isin(markings.gold_members)]
    golddata = golddata[golddata.marking == 'fan']
    # citizens = set(p4img.data.user_name) - set(markings.gold_members)

    # create plot window
    fig, ax = plt.subplots(ncols=2, nrows=2, figsize=(12, 10))
    fig.tight_layout()
    axes = ax.flatten()

    # fill images, 0 and 2 get it automatically
    for i in [1, 3]:
        p4img.show_subframe(ax=axes[i])

    # remove pixel coord axes
    for ax in axes:
        ax.axis('off')

    # citizen stuff
    p4img.plot_fans(ax=axes[0])
    axes[0].set_title('Citizen Markings')
    DBScanner(p4img.get_fans(), 'fan', ax=axes[1], eps=7, min_samples=5,
              linestyle='-')
    axes[1].set_title('All citizens clusters (including science team)')

    # gold stuff
    gold_star_plotter(p4img, axes[2], fans=True, blotches=False)
    axes[2].set_title('Science team markings')
    DBScanner(golddata, 'fan', ax=axes[1], min_samples=2, eps=11,
              linestyle='--')
    axes[3].set_title('Science team clusters')

    plt.show()


if __name__ == '__main__':
    main()
