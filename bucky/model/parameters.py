"""Submodule to handle the model parameterization and randomization"""
import copy
import logging
from pprint import pformat

import numpy  # we only need numpy for interp, we could write this in cupy...
import yaml

from ..numerical_libs import reimport_numerical_libs, xp
from ..util import dotdict
from ..util.distributions import approx_mPERT_sample, truncnorm


def calc_Te(Tg, Ts, n, f):
    """Calculate the latent period"""
    num = 2.0 * n * f / (n + 1.0) * Tg - Ts
    den = 2.0 * n * f / (n + 1.0) - 1.0
    return num / den


def calc_Reff(m, n, Tg, Te, r):
    """Calculate the effective reproductive number"""
    num = 2.0 * n * r / (n + 1.0) * (Tg - Te) * (1.0 + r * Te / m) ** m
    den = 1.0 - (1.0 + 2.0 * r / (n + 1.0) * (Tg - Te)) ** (-n)
    return num / den


def calc_Ti(Te, Tg, n):
    """Calcuate the infectious period"""
    return (Tg - Te) * 2.0 * n / (n + 1.0)


def calc_beta(Te):
    """Derive beta from Te"""
    return 1.0 / Te


def calc_gamma(Ti):
    """Derive gamma from Ti"""
    return 1.0 / Ti


def CI_to_std(CI):
    """Convert a 95% confidence interval to an equivilent stddev (assuming its normal)"""
    lower, upper = CI
    std95 = xp.sqrt(1.0 / 0.05)
    return (upper + lower) / 2.0, (upper - lower) / std95 / 2.0


class buckyParams:
    """Class holding all the model parameters defined in the par file, also used to reroll them for each MC run"""

    def __init__(self, par_file=None):
        """Initialize the class, sync up the libs with the parent context and load the par file"""

        reimport_numerical_libs("model.parameters.buckyParams.__init__")

        self.par_file = par_file
        if par_file is not None:
            self.base_params = self.read_yml(par_file)

            for k, v in self.base_params.items():
                if type(v) == dict:
                    p = k
                    if "values" in self.base_params[p]:
                        # params[p] = np.array(base_params[p]["values"])
                        # params[p] *= truncnorm(1.0, var, size=params[p].shape, a_min=1e-6)
                        # interp to our age bins
                        if self.base_params[p]["age_bins"] != self.base_params["consts"]["age_bins"]:
                            self.base_params[p]["values"] = self.age_interp(
                                self.base_params["consts"]["age_bins"],
                                self.base_params[p]["age_bins"],
                                self.base_params[p]["values"],
                            )
                            self.base_params[p]["age_bins"] = self.base_params["consts"]["age_bins"]

                    for k2 in v:
                        if k2 == "values" or k2 == "mean":
                            self.base_params[k][k2] = xp.array(self.base_params[k][k2])

            self.consts = dotdict(self.base_params["consts"])
        else:
            self.base_params = None

    @staticmethod
    def read_yml(par_file):
        """Read in the YAML par file"""
        # TODO check file exists
        with open(par_file, "rb") as f:
            return yaml.load(f, yaml.SafeLoader)  # nosec

    def generate_params(self, var=0.2):
        """Generate a new set of params by rerolling, adding the derived params and rejecting invalid sets"""
        if var is None:
            var = 0.0
        while True:  # WTB python do-while...
            params = self.reroll_params(self.base_params, var)
            params = self.calc_derived_params(params)
            if (params.Te > 1.0 and params.Tg > params.Te and params.Ti > 3.0) or var == 0.0:
                return params
            logging.debug("Rejected params: " + pformat(params))

    def reroll_params(self, base_params, var):
        """Reroll the parameters defined in the par file"""
        params = dotdict({})
        for p in base_params:
            # Scalars
            if "gamma" in base_params[p]:
                mu = copy.deepcopy(base_params[p]["mean"])
                params[p] = approx_mPERT_sample(xp.array([mu]), gamma=base_params[p]["gamma"])

            elif "mean" in base_params[p]:
                if "CI" in base_params[p]:
                    if var:
                        params[p] = truncnorm(*CI_to_std(base_params[p]["CI"]), a_min=1e-6)
                    else:  # just use mean if we set var to 0
                        params[p] = copy.deepcopy(base_params[p]["mean"])
                else:
                    params[p] = xp.atleast_1d(copy.deepcopy(base_params[p]["mean"]))
                    params[p] *= truncnorm(loc=1.0, scale=var, a_min=1e-6)

            # age-based vectors
            elif "values" in base_params[p]:
                params[p] = xp.array(base_params[p]["values"])
                params[p] *= truncnorm(1.0, var, size=params[p].shape, a_min=1e-6)
                # interp to our age bins
                if base_params[p]["age_bins"] != base_params["consts"]["age_bins"]:
                    params[p] = self.age_interp(
                        base_params["consts"]["age_bins"],
                        base_params[p]["age_bins"],
                        params[p],
                    )

            # fixed values (noop)
            else:
                params[p] = copy.deepcopy(base_params[p])

            # clip values
            if "clip" in base_params[p]:
                clip_range = base_params[p]["clip"]
                params[p] = xp.clip(params[p], clip_range[0], clip_range[1])

        return params

    @staticmethod
    def age_interp(x_bins_new, x_bins, y):
        """Interpolate parameters define in age groups to a new set of age groups"""
        # TODO we should probably account for population for the 65+ type bins...
        x_mean_new = numpy.mean(numpy.array(x_bins_new), axis=1)
        x_mean = numpy.mean(numpy.array(x_bins), axis=1)
        return numpy.interp(x_mean_new, x_mean, y)

    @staticmethod
    def rescale_doubling_rate(D, params, A_diag=None):
        """Rescale parameters to match the input doubling times"""
        # TODO rename D to Td everwhere for consistency
        r = xp.log(2.0) / D
        params["R0"] = calc_Reff(
            params["consts"]["Im"],
            params["consts"]["En"],
            params["Tg"],
            params["Te"],
            r,
        )
        params["BETA"] = params["R0"] * params["GAMMA"]
        if A_diag is not None:
            # params['BETA'] /= xp.sum(A,axis=1)
            params["BETA"] /= A_diag
        return params

    @staticmethod
    def calc_derived_params(params):
        """Add the derived params that are calculated from the rerolled ones"""
        params["Te"] = calc_Te(
            params["Tg"],
            params["Ts"],
            params["consts"]["En"],
            params["frac_trans_before_sym"],
        )
        params["Ti"] = calc_Ti(params["Te"], params["Tg"], params["consts"]["En"])
        r = xp.log(2.0) / params["D"]
        params["R0"] = calc_Reff(
            params["consts"]["Im"],
            params["consts"]["En"],
            params["Tg"],
            params["Te"],
            r,
        )

        params["SIGMA"] = 1.0 / params["Te"]
        params["GAMMA"] = 1.0 / params["Ti"]
        params["BETA"] = params["R0"] * params["GAMMA"]
        params["SYM_FRAC"] = 1.0 - params["ASYM_FRAC"]
        params["THETA"] = 1.0 / params["H_TIME"]
        params["GAMMA_H"] = 1.0 / params["I_TO_H_TIME"]
        return params