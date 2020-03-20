"""The one-line interface to this library.

Do not import this module directly, but rather import the main ennemi module.
"""

import concurrent.futures
import itertools
import numpy as np
import sys
from ._entropy_estimators import _estimate_single_mi, _estimate_conditional_mi

def estimate_mi(y : np.ndarray, x : np.ndarray, lag = 0, *,
                k : int = 3, cond : np.ndarray = None, cond_lag : int = 0,
                mask : np.ndarray = None, parallel : str = None):
    """Estimate the mutual information between y and each x variable.
 
    Returns the estimated mutual information (in nats) for continuous
    variables. The result is a 2D array where the first index represents `x`
    rows and the second index represents the `lag` values.

    The time lag is interpreted as `y(t + lag) ~ x(t)`.
    The time lags are applied to the `x` and `cond` arrays such that the `y`
    array stays the same every time.
    This means that `y` is cropped to `y[max_lag:N+min(min_lag, 0)]`.

    If the `cond` parameter is set, conditional mutual information is estimated.
    The `cond_lag` parameter is added to the lag for the `cond` array.

    If the `mask` parameter is set, only those `y` observations with the
    matching mask element set to `True` are used for estimation.
    
    If the data set contains discrete variables or many identical
    observations, this method may return incorrect results or `-inf`.
    In that case, add low-amplitude noise to the data and try again.

    The calculation is based on Kraskov et al. (2004): Estimating mutual
    information. Physical Review E 69. doi:10.1103/PhysRevE.69.066138

    Positional or keyword parameters:
    ---
    y : array_like
        A 1D array of observations.
    x : array_like
        A 1D or 2D array where the rows are one or more variables and the
        columns are observations. The number of columns must be the same as in y.
    lag : int or array_like
        A time lag or 1D array of time lags to apply to x. Default 0.
        The values may be any integers with magnitude
        less than the number of observations.

    Optional keyword parameters:
    ---
    k : int
        The number of neighbors to consider. Default 3.
        Must be smaller than the number of observations left after cropping.
    cond : array_like
        A 1D array of observations used for conditioning.
        Must have as many observations as y.
    cond_lag : int
        Additional lag applied to the cond array. Default 0.
    mask : array_like or None
        If specified, an array of booleans that gives the y elements to use for
        estimation. Use this to exclude some observations from consideration
        while preserving the time series structure of the data. Elements of
        `x` and `cond` are masked with the lags applied. The length of this
        array must match the length `y`.
    parallel : str or None
        Whether to run the estimation in multiple processes. If None (the default),
        a heuristic will be used for the decision. If "always", each
        variable / time lag combination will be run in a separate subprocess,
        with as many concurrent processes as there are processors.
        If "disable", the combinations are estimated sequentially in the current process.
    """

    # The code below assumes that lag is an array
    if (isinstance(lag, int)):
        lag = [lag]
    lag = np.asarray(lag)

    # If x or y is a Python list, convert it to an ndarray
    # Keep the original x parameter around for the Pandas data frame check
    original_x = x
    x = np.asarray(x)
    y = np.asarray(y)
    if cond is not None:
        cond = np.asarray(cond)

    _check_parameters(x, y, k, cond, mask)

    # These are used for determining the y range to use
    min_lag = min(np.min(lag), np.min(lag+cond_lag))
    max_lag = max(np.max(lag), np.max(lag+cond_lag))

    # Validate that the lag is not too large
    if max_lag - min_lag >= y.size or max_lag >= y.size or min_lag <= -y.size:
        raise ValueError("lag is too large, no observations left")
    
    if x.ndim == 1:
        nvar = 1
    else:
        _, nvar = x.shape

    # Create a list of all variable, time lag combinations
    # The params map contains tuples for simpler passing into subprocess
    indices = list(itertools.product(range(len(lag)), range(nvar)))
    if x.ndim == 1:
        params = map(lambda lag: (x, y, lag, max_lag, min_lag, k, mask, cond, cond_lag), lag)
    else:
        params = map(lambda i: (x[:,i[1]], y, lag[i[0]], max_lag, min_lag, k, mask, cond, cond_lag), indices)

    # If there is benefit in doing so, and the user has not overridden the
    # heuristic, execute the estimation in multiple parallel processes
    if _should_be_parallel(parallel, indices, y):
        with concurrent.futures.ProcessPoolExecutor() as executor:
            conc_result = executor.map(_lagged_mi, params)
    else:
        conc_result = map(_lagged_mi, params)
    
    # Collect the results to a 2D array
    result = np.empty((len(lag), nvar))
    for index, res in zip(indices, conc_result):
        result[index] = res

    # If the input was a pandas data frame, set the column names
    if "pandas" in sys.modules:
        import pandas
        if isinstance(original_x, pandas.DataFrame):
            result = pandas.DataFrame(result, index=lag, columns=original_x.columns)
        elif isinstance(original_x, pandas.Series):
            result = pandas.DataFrame(result, index=lag, columns=[original_x.name])
        
    return result


def _check_parameters(x, y, k, cond, mask):
    # TODO: Validate that y and cond and mask are one-dimensional

    # Validate the array lengths
    if (x.shape[0] != len(y)):
        raise ValueError("x and y must have same length")
    if (cond is not None) and (x.shape[0] != len(cond)):
        raise ValueError("x and cond must have same length")
    if (x.shape[0] <= k):
        raise ValueError("k must be smaller than number of observations")

    # Validate the mask
    if mask is not None:
        mask = np.asarray(mask)
        if len(mask) != len(y):
            raise ValueError("mask length does not match y length")
        if mask.dtype != np.bool:
            raise TypeError("mask must contain only booleans")


def _should_be_parallel(parallel : str, indices : list, y : np.ndarray):
    # Check whether the user has forced a certain parallel mode
    if parallel == "always":
        return True
    elif parallel == "disable":
        return False
    elif parallel is not None:
        raise ValueError("unrecognized value for parallel argument")
    else:
        # As the user has not overridden the choice, use a heuristic
        # TODO: In a many variables/lags, small N case, it may make sense to
        #       use multiple processes, but batch the tasks
        return len(indices) > 1 and len(y) > 200


def _lagged_mi(param_tuple):
    # Unpack the param tuple used for possible cross-process transfer
    x, y, lag, max_lag, min_lag, k, mask, cond, cond_lag = param_tuple

    # The x observations start from max_lag - lag
    xs = x[max_lag-lag : len(x)-lag+min(min_lag, 0)]
    # The y observations always start from max_lag
    ys = y[max_lag : len(y)+min(min_lag, 0)]

    # Mask the observations if necessary
    if mask is not None:
        mask_subset = mask[max_lag : len(y)+min(min_lag, 0)]
        xs = xs[mask_subset]
        ys = ys[mask_subset]
    
    if cond is None:
        return _estimate_single_mi(xs, ys, k)
    else:
        # The cond observations have their additional lag term
        zs = cond[max_lag-(lag+cond_lag) : len(cond)-(lag+cond_lag)+min(min_lag, 0)]
        if mask is not None:
            zs = zs[mask_subset]

        return _estimate_conditional_mi(xs, ys, zs, k)
