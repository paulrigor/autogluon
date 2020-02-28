import gc, os, multiprocessing, logging
import numpy as np
import pandas as pd
from collections import defaultdict
from pandas import DataFrame, Series
from datetime import datetime
from sklearn.model_selection import KFold, StratifiedKFold, RepeatedKFold, RepeatedStratifiedKFold, train_test_split

from .constants import BINARY, REGRESSION, MULTICLASS, SOFTCLASS
from ..utils.savers import save_pd
from ..utils.decorators import calculate_time
from ...try_import import try_import_lightgbm

logger = logging.getLogger(__name__)


def get_pred_from_proba(y_pred_proba, problem_type=BINARY):
    if problem_type == BINARY:
        y_pred = [1 if pred >= 0.5 else 0 for pred in y_pred_proba]
    elif problem_type == REGRESSION:
        y_pred = y_pred_proba
    else:
        y_pred = np.argmax(y_pred_proba, axis=1)
    return y_pred


def generate_kfold(X, y=None, n_splits=5, random_state=0, stratified=False, n_repeats=1):
    kfolds = []
    if stratified and (y is not None):
        if n_repeats > 1:
            kf = RepeatedStratifiedKFold(n_splits=n_splits, n_repeats=n_repeats, random_state=random_state)
        else:
            kf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)

        kf.get_n_splits(X, y)
        for train_index, test_index in kf.split(X, y):
            kfolds.append([train_index, test_index])
    else:
        if n_repeats > 1:
            kf = RepeatedKFold(n_splits=n_splits, n_repeats=n_repeats, random_state=random_state)
        else:
            kf = KFold(n_splits=n_splits, shuffle=True, random_state=random_state)
        kf.get_n_splits(X)
        for train_index, test_index in kf.split(X):
            kfolds.append([train_index, test_index])
    return kfolds


def generate_train_test_split(X: DataFrame, y: Series, problem_type: str, test_size: float = 0.1, random_state=42) -> (DataFrame, DataFrame, Series, Series):
    if (test_size <= 0.0) or (test_size >= 1.0):
        raise ValueError("fraction of data to hold-out must be specified between 0 and 1")
    if problem_type in [REGRESSION, SOFTCLASS]:
        stratify = None
    else:
        stratify = y

    # TODO: Enable stratified split when y class would result in 0 samples in test.
    #  One approach: extract low frequency classes from X/y, add back (1-test_size)% to X_train, y_train, rest to X_test
    #  Essentially stratify the high frequency classes, random the low frequency (While ensuring at least 1 example stays for each low frequency in train!)
    #  Alternatively, don't test low frequency at all, trust it to work in train set. Risky, but highest quality for predictions.
    X_train, X_test, y_train, y_test = train_test_split(X, y.values, test_size=test_size, shuffle=True, random_state=random_state, stratify=stratify)
    if problem_type != SOFTCLASS:
        y_train = pd.Series(y_train, index=X_train.index)
        y_test = pd.Series(y_test, index=X_test.index)
    else:
        y_train = pd.DataFrame(y_train, index=X_train.index)
        y_test = pd.DataFrame(y_test, index=X_test.index)
    return X_train, X_test, y_train, y_test


# TODO: Move to lgb
def construct_dataset(x: DataFrame, y: Series, location=None, reference=None, params=None, save=False, weight=None):
    try_import_lightgbm()
    import lightgbm as lgb
    # save_pd.save(path=location + '.csv', df=x, header=False)
    feature_list = list(x.columns.values)
    # dataset = lgb.Dataset(data=location + '.csv', label=y, reference=reference, feature_name=feature_list)
    dataset = lgb.Dataset(data=x, label=y, reference=reference, free_raw_data=True, params=params, weight=weight)

    if save:
        if os.path.exists(location + '.bin'):
            os.remove(location + '.bin')
        else:
            pass

        os.makedirs(os.path.dirname(location + '.bin'), exist_ok=True)
        dataset.save_binary(location + '.bin')
        # dataset_binary = lgb.Dataset(location + '.bin', reference=reference, free_raw_data=False)# .construct()


    return dataset


# TODO: Move to lgb
def construct_dataset_low_memory(X: DataFrame, y: Series, location, reference=None, params=None):
    try_import_lightgbm()
    import lightgbm as lgb
    cat_columns = list(X.select_dtypes(include='category').columns.values)
    # X = X.drop(columns_categorical, axis=1)

    X[cat_columns] = X[cat_columns].apply(lambda x: x.cat.codes)

    columns = list(X.columns)
    for column in columns:
        column_data = X[column]

    split_train = len(X)
    # split_train = 11111111  # border between train/test in pickle (length of our train)
    n_attrs = len(X.columns)
    # n_attrs = 25  # as is

    pickle_list = [X]
    # pickle_list = ['attrs_xxxx', 'attrs_base_cnt', 'attrs_nunique']  # list of pickled attrs
    # del_cols = ['click_time', 'day', ]  # attrs to be deleted in final train


    si = 0
    os.makedirs(os.path.dirname(location + '.mmp'), exist_ok=True)
    mmap = np.memmap(location + '.mmp', dtype='float32', mode='w+', shape=(split_train, n_attrs))


    columns = []
    for pkl in pickle_list:
        _temp = pkl
        # _temp = load_attrs(pkl)

        _columns = [x for x in _temp.columns]
        columns = columns + _columns

        nodel_ind = [_temp.columns.tolist().index(x) for x in _temp.columns]

        _temp = _temp.iloc[:split_train, nodel_ind]

        ei = _temp.values.shape[1]
        mmap[:, si:si+ei] = _temp.values
        si += ei

        del _temp
        gc.collect()

    mmap.flush()
    del mmap
    gc.collect()

    mmap = np.memmap(location + '.mmp', dtype='float32', mode='r', shape=(split_train, n_attrs))
    _train = np.array(mmap[:split_train])
    # _val = np.array(mmap[split_train:])

    # _train = _train[:, columns.index('is_attributed')]
    # _val = _val[:, columns.index('is_attributed')]

    use_columns = columns
    # muse_columns = [columns.index(x) for x in use_columns]

    # d_train = _train[:, muse_columns]
    xgtrain = lgb.Dataset(_train, label=y, params=params, reference=reference, categorical_feature=cat_columns, feature_name=columns)
    # d_val = _val[:, muse_columns]
    # xgvalid = lgb.Dataset(d_val, label=y_test, reference=xgtrain, **params)

    # bst = lgb.train(model_params, xgtrain, valid_sets=[xgvalid], valid_names=['valid'], evals_result=evals_results, **fit_params)

    return xgtrain


# TODO: Move to lgb
@calculate_time
def construct_dataset_lowest_memory(X: DataFrame, y: Series, location, reference=None, params=None):

    try_import_lightgbm()
    import lightgbm as lgb
    cat_columns = list(X.select_dtypes(include='category').columns.values)

    columns = list(X.columns)
    X[cat_columns] = X[cat_columns].apply(lambda x: x.cat.codes)

    cat_columns_index = [columns.index(cat) for cat in cat_columns]

    logger.log(15, 'Saving... '+str(location)+'.csv')
    save_pd.save(path=location + '.csv', df=X, header=False, index=True)

    xgtrain = lgb.Dataset(location + '.csv', label=y, params=params, reference=reference, categorical_feature=cat_columns_index,
                          feature_name=columns,
                          )

    return xgtrain


def convert_categorical_to_int(X):
    X = X.copy()
    cat_columns = X.select_dtypes(['category']).columns
    X[cat_columns] = X[cat_columns].apply(lambda x: x.cat.codes)
    return X


def setup_outputdir(output_directory):
    if output_directory is None:
        utcnow = datetime.utcnow()
        timestamp = utcnow.strftime("%Y%m%d_%H%M%S")
        output_directory = "AutogluonModels/ag-" + timestamp + '/'
        os.makedirs(output_directory)
        logger.log(25, "No output_directory specified. Models will be saved in: %s" % output_directory)
    output_directory = os.path.expanduser(output_directory)  # replace ~ with absolute path if it exists
    if output_directory[-1] != '/':
        output_directory = output_directory + '/'
    return output_directory


def setup_compute(nthreads_per_trial, ngpus_per_trial):
    if nthreads_per_trial is None:
        nthreads_per_trial = multiprocessing.cpu_count()  # Use all of processing power / trial by default. To use just half: # int(np.floor(multiprocessing.cpu_count()/2))
    if ngpus_per_trial is None:
        ngpus_per_trial = 0 # do not use GPU by default
    if ngpus_per_trial > 1:
        ngpus_per_trial = 1
        logger.debug("tabular_prediction currently doesn't use >1 GPU per training run. ngpus_per_trial set = 1")
    return nthreads_per_trial, ngpus_per_trial


def setup_trial_limits(time_limits, num_trials, hyperparameters={'NN': None}):
    """ Adjust default time limits / num_trials """
    if num_trials is None:
        if time_limits is None:
            time_limits = 10 * 60  # run for 10min by default
        time_limits /= float(len(hyperparameters.keys()))  # each model type gets half the available time
        num_trials = 1000  # run up to 1000 trials (or as you can within the given time_limits)
    elif time_limits is None:
        time_limits = int(1e6)  # user only specified num_trials, so run all of them regardless of time-limits
    else:
        time_limits /= float(len(hyperparameters.keys()))  # each model type gets half the available time
    if time_limits <= 10:  # threshold = 10sec, ie. too little time to run >1 trial.
        num_trials = 1
    time_limits *= 0.9  # reduce slightly to account for extra time overhead
    return time_limits, num_trials


def dd_list():
    return defaultdict(list)

def normalize_pred_probas(y_predprob, problem_type, min_pred=0.0, max_pred=1.0, eps=1e-7):
    """ Clips predicted probabilities to ensure there are no zeros (eg. for log-loss).
        Will also ensure no predicted probability exceeds [0,1].
        Args:
            y_predprob: 1D (binary classification) or 2D (multi-class) numpy array of predicted probabilities
            problem_type: must be BINARY, MULTICLASS, or SOFTCLASS
            min_pred: minimum prediction value that may be encountered (for BINARY only)
            max_pred: maximum prediction value that may be encountered (for BINARY only)
            eps: how far from 0 remapped predicted probabilities should be
    """
    if problem_type == BINARY:
        y_predprob = ((1 - 2*eps) * ((y_predprob - min_pred)/(max_pred - min_pred))) + eps
        return y_predprob
    elif problem_type in [MULTICLASS, SOFTCLASS]:
        y_predprob = y_predprob.asnumpy()
        most_negative_rowvals = np.clip(np.min(y_predprob, axis=1), a_min=None, a_max=0)
        y_predprob = y_predprob - most_negative_rowvals[:,None]  # ensure nonnegative rows
        y_predprob = np.clip(y_predprob, a_min=eps, a_max=None)  # ensure no zeros
        return y_predprob / y_predprob.sum(axis=1, keepdims=1)  # renormalize
    else:
        raise ValueError("problem_type must be either BINARY or MULTICLASS")


def combine_pred_and_true(y_predprob, y_true, upweight_factor=0.25):
    """ Used in distillation, combines true (integer) classes with 2D array of predicted probabilities.
        Returns new 2D array of predicted probabilities where true classes are upweighted by upweight_factor (and then probabilities are renormalized)
    """
    if len(y_predprob) != len(y_true):
        raise ValueError("y_predprob and y_true cannot have different lengths for distillation. Perhaps some classes' data was deleted during label cleaning.")

    y_trueprob = np.zeros((y_true.size, y_true.max()+1))
    y_trueprob[np.arange(y_true.size),y_true] = upweight_factor
    y_predprob = y_predprob + y_trueprob
    y_predprob = y_predprob / y_predprob.sum(axis=1, keepdims=1) # renormalize
    return y_predprob

