"""Managing clustering, fnotching and cut application here."""
from __future__ import division, print_function

import importlib
import logging
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from numpy.linalg import norm
from scipy.stats import circmean
from sklearn.preprocessing import minmax_scale, normalize, robust_scale, scale

from . import io, markings
from .dbscan import DBScanner, HDBScanner

importlib.reload(logging)
logpath = Path.home() / 'p4reduction.log'
logging.basicConfig(filename=str(logpath), filemode='w', level=logging.DEBUG,
                    format='%(asctime)s %(levelname)s: %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

matplotlib.style.use('bmh')


class NotEnoughMarkingData(Exception):
    def __init__(self):
        Exception.__init__(self, "Not enough data to cluster (< 3 items).")


class ClusteringManager(object):

    """Control class to manage the clustering pipeline.

    Parameters
    ----------
    dbname : str or pathlib.Path, optional
        Path to the database used by DBManager. If not provided, DBManager
        will find the most recent one and use that.
    fnotch_distance : int
        Parameter to control the distance below which fans and blotches are
        determined to be 2 markings of the same thing, creating a FNOTCH
        chimera. The ratio between blotch and fan marks determines a fnotch
        value that can be used as a discriminator for the final object catalog.
    eps : int
        Parameter to control the exclusion distance for the DBSCAN
    output_dir : str or pathlib.Path
        Path to folder where to store output. Default: io.data_root / 'output'
    output_format : {'hdf', 'csv', 'both'}
        Format to save the output in. Default: 'hdf'
    cut : float
        Value to apply for fnotch cutting.
    min_samples_factor : float
        Value to multiply the number of unique classifications per image_id with
        to determine the `min_samples` value for DBSCAN to use. Default: 0.1
    do_dynamic_min_samples : bool, default is False
        Switch to decide if `min_samples` is being dynamically calculated.

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
    current_coords : list
        List of coordinate columns currently used for clustering
    current_markings : pandas.DataFrame
        Dataframe with the currently to-be-clustered marking data
    reduced_data : dictionary
        Stores reduced average cluster markings
    n_clustered_fans
    n_clustered_blotches
    output_dir_clustered : pathlib.Path
        Path to full clustering results, without removal of fnotched clusters.
    cut_dir : pathlib.Path
        Path to final fan and blotch clusters, after applying `cut`.
    """
    scalers = {'norm': normalize,
               'robust': robust_scale,
               'minmax': minmax_scale,
               'scale': scale}

    def __init__(self, dbname=None, fnotch_distance=10, eps=10,
                 output_dir=None, output_format='csv', cut=0.5,
                 min_samples_factor=0.15,
                 include_angle=True, id_=None, pm=None,
                 include_distance=False, include_radius=False,
                 do_dynamic_min_samples=False,
                 quiet=True, normalize=False,
                 scaler='robust',
                 use_DBSCAN=True,
                 hdbscan_min_samples=None,
                 min_samples=None,
                 proba_cut=0.0):
        self.db = io.DBManager(dbname)
        self.dbname = self.db.dbname
        self.fnotch_distance = fnotch_distance
        self.output_dir = output_dir
        self.output_format = output_format
        self.eps = eps
        self.cut = cut
        self.include_angle = include_angle
        self.include_distance = include_distance
        self.include_radius = include_radius
        self.confusion = []
        self.min_samples_factor = min_samples_factor
        self.do_dynamic_min_samples = do_dynamic_min_samples
        self.quiet = quiet
        self.normalize = normalize
        self.scaler = scaler
        self.use_DBSCAN = use_DBSCAN
        self.hdbscan_min_samples = hdbscan_min_samples
        self.min_samples = min_samples
        self.proba_cut = proba_cut

        # to be defined at runtime:
        self.current_coords = None
        self.current_markings = None
        self.reduced_data = None
        self.fnotches = None
        self.fnotched_blotches = None
        self.fnotched_fans = None
        self.p4id = None
        self.newfans = None
        self.newblotches = None

        if pm is not None:
            self.pm = pm
        else:
            self.pm = io.PathManager(output_dir, id_=id_,
                                     suffix='.'+self.output_format)

    @property
    def n_clustered_fans(self):
        """int : Number of clustered fans."""
        return len(self.clustered_data['fan'])

    @property
    def n_clustered_blotches(self):
        """int : Number of clustered blotches."""
        return len(self.clustered_data['blotch'])

    def pre_processing(self):
        """Preprocess before clustering.

        Depending on the flags used when constructing this manager,
        different columns end up being clustered on.

        Parameters
        ----------
        data : pd.DataFrame
            Dataframe with data to cluster on
        kind : str
            String indicating if to cluster on blotch or fan data
        """

        # add unit circle coordinates for angles
        angles = self.marking_data['angle']
        marking_data = self.marking_data.assign(xang=np.cos(np.deg2rad(angles)))
        marking_data = marking_data.assign(yang=np.sin(np.deg2rad(angles)))

        if len(marking_data) < 3:
            return None

        coords = ['x', 'y']

        # now marking kind dependent additions:
        if self.kind == 'fan':
            if self.include_distance:
                coords.append('distance')
            if self.include_angle:
                coords += ['xang', 'yang']

        elif self.kind == 'blotch':
            if self.include_radius:
                coords += ['radius_1', 'radius_2']
            if self.include_angle:
                coords.append('yang')
        # Determine the clustering input matrix
        if self.normalize:
            f = self.scalers[self.scaler]
            current_X = f(marking_data[coords].values, axis=0)
        else:
            current_X = marking_data[coords].values

        # store stuff for later
        self.current_coords = coords
        self.current_markings = marking_data
        return current_X

    def post_processing(self):
        """Create mean objects out of cluster label members.

        Note: I take the image_id of the marking of the first member of
        the cluster as image_id for the whole cluster. In rare circumstances,
        this could be wrong for clusters in the overlap region.

        Stores output in self.reduced_data dictionary
        """
        kind = self.kind
        Marking = markings.Fan if kind == 'fan' else markings.Blotch
        cols = Marking.to_average

        reduced_data = []
        data = self.current_markings
        for cluster_members in self.clusterer.clustered_data:
            if self.use_DBSCAN:
                clusterdata = data[cols+['user_name']].iloc[cluster_members]
            else:
                clusterdata = data.loc[cluster_members, cols+['user_name']]
            # if the same user is inside one cluster, just take
            # the first entry per user:
            filtered = clusterdata.groupby('user_name').first()
            meandata = self.get_average_object(filtered)
            cluster = Marking(meandata, scope='planet4')
            # storing n_members into the object for later.
            cluster.n_members = len(cluster_members)
            # storing this saved marker for later in ClusteringManager
            cluster.saved = False
            cluster.image_id = self.pm.id_

            reduced_data.append(cluster)

        self.reduced_data[kind] = reduced_data
        if not self.quiet:
            print("Reduced data to %i %s(e)s." % (len(reduced_data), kind))
        logging.debug("Reduced data to %i %s(e)s.", len(reduced_data), kind)

    def get_average_object(self, clusterdata):
        "Create the average object out of a cluster of data."
        meandata = clusterdata.mean()
        # this determines the upper limit for circular mean
        high = 180 if self.kind == 'blotch' else 360
        avg = circmean(clusterdata.angle, high=high)
        meandata.angle = avg
        return meandata

    def cluster_data(self):
        """Basic clustering.

        For each fan and blotch markings in `data` a DBScanner object is
        created that executes the actual clustering.
        To be able to apply dynamic calculation of `min_samples`, this will
        always be on 'planet4' tile coordinates.

        Parameters
        ----------
        data : pandas.DataFrame
            containing both fan and blotch data to be clustered.
        """
        logging.debug('ClusterManager: cluster_data()')
        # reset stored clustered data
        self.reduced_data = {}

        # Calculate the unique classification_ids so that the mininum number of
        # samples for DBScanner can be calculated (15 % currently)
        # use only class_ids that actually contain fan and blotch markings
        f1 = self.data.marking == 'fan'   # this creates a boolean filter
        f2 = self.data.marking == 'blotch'
        # combine filters with logical OR:
        n_classifications = self.data[f1 | f2].classification_id.nunique()

        if self.do_dynamic_min_samples:
            min_samples = round(self.min_samples_factor * n_classifications)
            # ensure that min_samples is at least 3:
            min_samples = max(min_samples, 3)
            self.min_samples = min_samples
        elif self.min_samples is None:
            # 3 turned out to be a well working default min_samples requirement
            min_samples = 3
            self.min_samples = min_samples
        else:
            min_samples = self.min_samples

        for kind in ['fan', 'blotch']:
            # what is included for clustering is decided in pre_processing
            self.marking_data = self.data[self.data.marking == kind]
            self.kind = kind
            current_X = self.pre_processing()
            if current_X is not None:
                if self.use_DBSCAN:
                    clusterer = DBScanner(current_X, eps=self.eps,
                                          min_samples=min_samples)
                else:
                    clusterer = HDBScanner(current_X,
                                           min_cluster_size=min_samples,
                                           min_samples=self.hdbscan_min_samples,
                                           proba_cut=self.proba_cut)
                # store the scanner object in both cases into `self`
                self.clusterer = clusterer
            else:
                # current_X is empty so store empty results and skip to next `kind`
                self.reduced_data[kind] = []
                continue
            # storing of clustered data happens in here:
            self.post_processing()
            self.confusion.append((self.pm.id_, kind,
                                   len(self.current_markings),
                                   len(self.reduced_data[kind]),
                                   clusterer.n_rejected))
        self.n_classifications = n_classifications
        if not self.quiet:
            print("n_classifications:", self.n_classifications)
            print("min_samples:", self.min_samples)

    def do_the_fnotch(self):
        """Combine fans and blotches if necessary.

        Use `fnotch_distance` as criterion for linear algebraic distance between
        average cluster markings to determine if they belong to a Fnotch, a
        chimera object of indecision between a Fan and a Blotch, to be decided
        later in the process by applying a `cut` on the resulting Fnotch
        objects.

        See Also
        --------
        markings.Fnotch : The Fnotch object with a `get_marking` method for a
            `cut` value.
        """
        # check first if both blotchens and fans were found, if not, we don't
        # need to fnotch.
        if not all(self.reduced_data.values()):
            logging.debug("CM: no fnotching required.")
            self.fnotches = []
            self.fnotched_blotches = self.reduced_data['blotch']
            self.fnotched_fans = self.reduced_data['fan']
            return

        logging.debug("CM: do_the_fnotch")
        n_close = 0
        fnotches = []
        blotches = []
        fans = []
        for blotch in self.reduced_data['blotch']:
            for fan in self.reduced_data['fan']:
                delta = blotch.center - fan.midpoint
                if norm(delta) < self.fnotch_distance:
                    fnotch_value = calc_fnotch(fan.n_members, blotch.n_members)
                    fnotch = markings.Fnotch(fnotch_value, fan, blotch,
                                             scope='hirise')
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
        logging.debug("CM: do_the_fnotch: Found %i fnotches.", n_close)

    # def execute_pipeline(self, data):
    #     """Execute the standard list of methods for catalog production.
    #
    #     Parameters
    #     ----------
    #     data : pandas.DataFrame
    #         The dataframe containing the data to be clustered.
    #     """
    #     self.cluster_data(data)
    #     # self.do_the_fnotch()
    #     # self.apply_fnotch_cut()
    #     self.store_clustered()
    #     # self.store_fnotched()

    def cluster_image_id(self, image_id, data=None):
        """Process the clustering for one image_id.

        Parameters
        ----------
        image_id : str
            Planetfour `image_id`
        data : pd.DataFrame, optional
            Dataframe with data for this clustering run
        """
        image_id = io.check_and_pad_id(image_id)
        logging.info("Clustering data for %s", image_id)
        self.pm.id_ = image_id
        if data is None:
            self.data = self.db.get_image_id_markings(image_id)
        else:
            self.data = data.copy()
        self.cluster_data()
        logging.debug("Clustering completed.")
        self.store_clustered()

    def cluster_image_name(self, image_name, data=None):
        """Process the clustering and fnotching pipeline for a HiRISE image_name.

        Parameters
        ----------
        image_name : str
            HiRISE image_name (= obsid in HiLingo) to cluster on.
            Used for storing the data in `obsid` indicated subfolders.
        data : pd.DataFrame
            Dataframe containing the data to cluster on.
        """
        logging.info("Clustering data for %s", image_name)
        if data is None:
            namedata = self.db.get_image_name_markings(image_name)
        else:
            namedata = data.copy()
        image_ids = namedata.image_id.unique()
        self.pm = io.PathManager(self.output_dir / image_name,
                                 id_=image_ids[0], suffix='.'+self.output_format)
        for image_id in image_ids:
            self.pm.id_ = image_id
            self.data = data[data.image_id == image_id]
            self.cluster_data()
            self.store_clustered()

    def cluster_obsid(self, *args, **kwargs):
        "Alias to cluster_image_name."
        self.cluster_image_name(*args, **kwargs)

    def store_fnotched(self):
        """Write out the clustered and fnotched data."""
        logging.debug('CM: Writing output files.')
        logging.debug('CM: Output dir: %s', self.datapath)

        # first write the fnotched data
        for outfname, outdata in zip(['fnotchfile', 'blotchfile', 'fanfile'],
                                     [self.fnotches, self.fnotched_blotches,
                                      self.fnotched_fans]):
            if len(outdata) == 0:
                continue
            # get the path from PathManager object
            series = [cluster.store() for cluster in outdata]
            df = pd.DataFrame(series)
            self.save(df, getattr(self.pm, outfname))

    def store_clustered(self):
        "Store the unfnotched data."
        outdir = self.pm.output_dir_clustered
        outdir.mkdir(exist_ok=True)
        for outfname, outdata in zip(['reduced_blotchfile', 'reduced_fanfile'],
                                     [self.reduced_data['blotch'],
                                      self.reduced_data['fan']]):
            if len(outdata) == 0:
                continue
            series = [cluster.store() for cluster in outdata]
            df = pd.DataFrame(series)
            self.save(df, getattr(self.pm, outfname))

    def cluster_all(self):
        image_names = self.db.image_names
        for image_name in image_names:
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

    def get_newfans_newblotches(self):
        logging.debug("Executing get_newfans_newblotches")
        df = self.pm.fnotchdf

        # check if we got a fnotch dataframe. If not, we assume none were found.
        if df is None:
            logging.debug("No fnotches found on disk.")
            self.newfans = []
            self.newblotches = []
            return

        # apply Fnotch method `get_marking` with given cut.
        fnotches = df.apply(markings.Fnotch.from_series, axis=1,
                            args=('hirise',))
        final_clusters = fnotches.apply(lambda x: x.get_marking(self.cut))

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

    def apply_fnotch_cut(self, cut=None):
        logging.debug("Executing apply_fnotch_cut")
        if cut is None:
            cut = self.cut

        # storage path for the final catalog after applying `cut`
        # PathManager self.pm is doing that.
        self.pm.get_cut_folder(cut)

        self.get_newfans_newblotches()

        if len(self.newfans) > 0:
            newfans = self.newfans.apply(lambda x: x.store())
            try:
                completefans = pd.DataFrame(
                    self.pm.fandf).append(newfans, ignore_index=True)
            except OSError:
                completefans = newfans
            logging.debug("No of fans now: %i" % len(completefans))
        else:
            logging.debug("Apply fnotch cut: No new fans found.")
            completefans = self.pm.fandf
        if len(self.newblotches) > 0:
            newblotches = self.newblotches.apply(lambda x: x.store())
            try:
                completeblotches = pd.DataFrame(
                    self.pm.blotchdf).append(newblotches, ignore_index=True)
            except OSError:
                completeblotches = newblotches
            logging.debug("No of blotches now: %i" % len(completeblotches))
        else:
            logging.debug('Apply fnotch cut: no blotches survived.')
            completeblotches = self.pm.blotchdf
        self.save(completefans, self.pm.final_fanfile)
        self.save(completeblotches, self.final_blotchfile)
        logging.debug("Finished apply_fnotch_cut.")

    def save(self, obj, path):
        try:
            if self.output_format in ['hdf', 'both']:
                obj.to_hdf(str(path.with_suffix('.hdf')), 'df')
            if self.output_format in ['csv', 'both']:
                obj.to_csv(str(path.with_suffix('.csv')), index=False)
        # obj could be NoneType if no blotches or fans were found. Catching it here.
        except AttributeError:
            pass

    def save_confusion_data(self, fname):
        self.confusion_data.to_csv(fname)

######
# Functions
#####


def get_mean_position(fan, blotch, scope):
    """Calculate mean for just the base coordinates of some data."""
    if scope == 'hirise':
        columns = ['hirise_x', 'hirise_y']
    else:
        columns = ['x', 'y']

    df = pd.DataFrame([fan.data[columns], blotch.data[columns]])
    return df.mean()


def calc_fnotch(nfans, nblotches):
    """Calculate the fnotch value (or fan-ness)."""
    return (nfans) / (nfans + nblotches)


def gold_star_plotter(gold_id, axis, kind='blotches'):
    """Plot gold data."""
    for goldstar, color in zip(markings.gold_members,
                               markings.gold_plot_colors):
        if kind == 'blotches':
            gold_id.plot_blotches(user_name=goldstar, ax=axis,
                                  user_color=color)
        if kind == 'fans':
            gold_id.plot_fans(user_name=goldstar, ax=axis, user_color=color)
        markings.gold_legend(axis)


def is_catalog_production_good():
    """A simple quality check for the catalog production."""
    from pandas.core.index import InvalidIndexError
    db = io.DBManager(io.get_current_database_fname())
    not_there = []
    invalid_index = []
    value_error = []
    for image_name in db.image_names:
        try:
            io.PathManager(image_name)
        except InvalidIndexError:
            invalid_index.append(image_name)
        except ValueError:
            value_error.append(image_name)
    if len(value_error) == 0 and len(not_there) == 0 and\
            len(invalid_index) == 0:
        return True
    else:
        return False


def main():
    """Exeucute gold data plotting by default. Should probably moved elsewhere.

    Also, most likely not working currently.
    """
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
    # TODO: fix use syntax of DBScanner here
    # DBScanner(p4img.get_fans(), eps=7, min_samples=5)
    axes[1].set_title('All citizens clusters (including science team)')

    # gold stuff
    gold_star_plotter(p4img, axes[2], kind='fans')
    axes[2].set_title('Science team markings')
    # TODO: refactor for plotting version of DBSCanner
    # DBScanner(golddata, ax=axes[1], min_samples=2, eps=11,
    #           linestyle='--')
    axes[3].set_title('Science team clusters')

    plt.show()


if __name__ == '__main__':
    main()
