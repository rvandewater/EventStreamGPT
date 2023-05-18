from __future__ import annotations

import dataclasses
import enum
import functools
import json
import re
from collections.abc import Callable, Sequence
from importlib.util import find_spec
from pathlib import Path
from typing import Any, TypeVar, Union

import hydra
import numpy as np
import pandas as pd
import polars as pl

PROPORTION = float
COUNT_OR_PROPORTION = Union[int, PROPORTION]


def count_or_proportion(N: int | None, cnt_or_prop: COUNT_OR_PROPORTION) -> int:
    """Returns either `cnt_or_prop` if it is an integer of `int(N*cnt_or_prop)` if it is a float.

    Used for resolving cutoff variables that can either be passed as integer counts or fractions of
    a whole. E.g., the vocabulary should contain only elements that occur with count or proportion
    at least X, where X might be 20 times, or 1%.
    """
    if type(cnt_or_prop) is int:
        return cnt_or_prop
    assert N is not None
    if type(N) is int:
        return int(round(cnt_or_prop * N))
    elif type(N) is pl.Expr:
        return (N * cnt_or_prop).round(0).cast(int)
    else:
        raise TypeError(f"{type(N)} is invalid for `N`")


def lt_count_or_proportion(
    N_obs: int, cnt_or_prop: COUNT_OR_PROPORTION | None, N_total: int | None = None
) -> bool:
    if cnt_or_prop is None:
        return False
    return N_obs < count_or_proportion(N_total, cnt_or_prop)


def is_monotonically_nonincreasing(a: np.ndarray) -> bool:
    """Returns True if and only if a is monotonically non-increasing."""
    return np.all(a[:-1] >= a[1:])


def flatten_dict(exp_dict: dict[Sequence[str], Any]) -> dict[str, Any]:
    """Flattens a dictionary out by key.

    Args:
        `exp_dict` (`Dict[Sequence[Hashable], Any]`):
            A dictionary of the form `{[k1, k2, ...]: v}`

    Returns: A dictionary of the form `{k1: v, k2: v, ...}`
    """

    out = {}
    for ks, v in exp_dict.items():
        for k in ks:
            out[k] = v
    return out


def to_sklearn_np(vals: pd.Series):
    """Takes a pandas series and returns a view consisting of only non-null numeric types reshaped
    to a 2D view where the last dimension is 1 (which is needed for scikit-learn)."""
    vals = vals.dropna()
    vals = vals[vals.apply(lambda v: pd.api.types.is_numeric_dtype(type(v)))]
    vals = vals.astype(float)
    return vals.values.reshape((-1, 1))


class StrEnum(str, enum.Enum):
    """This is made obsolete by python 3.11, which has `enum.StrEnum` natively. TODO(mmd): Upgrade
    to python 3.11 and eliminate this class.

    This code is sourced from
    https://github.com/irgeek/StrEnum/blob/0f868b68cb7cdab50a79117679a301f550a324bc/strenum/__init__.py#L21

    StrEnum is a Python ``enum.Enum`` that inherits from ``str``. The default
    ``auto()`` behavior uses the member name as its value.
    Example usage::
        class Example(StrEnum):
            UPPER_CASE = auto()
            lower_case = auto()
            MixedCase = auto()
        assert Example.UPPER_CASE == "upper_case"
        assert Example.lower_case == "lower_case"
        assert Example.MixedCase == "mixedcase"
    """

    def __new__(cls, value, *args, **kwargs):
        if not isinstance(value, (str, enum.auto)):
            raise TypeError(f"Values of StrEnums must be strings: {value!r} is a {type(value)}")
        return super().__new__(cls, value, *args, **kwargs)

    def __str__(self):
        return str(self.value)

    def _generate_next_value_(name, *_):
        return name.lower()

    @classmethod
    def values(cls):
        return list(map(lambda c: c.value, cls))


# Create a generic variable that can be 'JSONableMixin', or any subclass.
JSONABLE_INSTANCE_T = TypeVar("JSONABLE_INSTANCE_T", bound="JSONableMixin")


class JSONableMixin:
    """A simple mixin to enable saving/loading of dataclasses (or other classes in theory) to json
    files."""

    @classmethod
    def from_dict(cls: type[JSONABLE_INSTANCE_T], as_dict: dict) -> JSONABLE_INSTANCE_T:
        """This is a default method that can be overwritten in derived classes."""
        return cls(**as_dict)

    def to_dict(self) -> dict[str, Any]:
        if dataclasses.is_dataclass(self):
            return dataclasses.asdict(self)
        raise NotImplementedError("This must be overwritten in non-dataclass derived classes!")

    def to_json_file(self, fp: Path, do_overwrite: bool = False):
        """Writes configuration object to a json file as a plain dictionary."""
        if (not do_overwrite) and fp.exists():
            raise FileExistsError(f"{fp} exists and do_overwrite = {do_overwrite}")
        with open(fp, mode="w") as f:
            json.dump(self.to_dict(), f)

    @classmethod
    def from_json_file(cls: type[JSONABLE_INSTANCE_T], fp: Path) -> JSONABLE_INSTANCE_T:
        """Build configuration object from contents of `fp` interpreted as a dictionary stored in
        json."""
        with open(fp) as f:
            return cls.from_dict(json.load(f))


def num_initial_spaces(s: str) -> int:
    out = 0
    while s.startswith(" "):
        out += 1
        s = s[1:]
    return out


def task_wrapper(task_func: Callable) -> Callable:
    """Optional decorator that controls the failure behavior when executing the task function.

    This wrapper can be used to:
    - make sure loggers are closed even if the task function raises an exception (prevents multirun failure)
    - save the exception to a `.log` file
    - mark the run as failed with a dedicated file in the `logs/` folder (so we can find and rerun it later)
    - etc. (adjust depending on your needs)

    Example:
    ```
    @utils.task_wrapper
    def train(cfg: DictConfig) -> Tuple[dict, dict]:

        ...

        return metric_dict, object_dict
    ```
    """

    @functools.wraps(task_func)
    def wrap(*args, **kwargs):
        try:
            fn_return = task_func(*args, **kwargs)
        except Exception as ex:
            # some hyperparameter combinations might be invalid or cause out-of-memory errors
            # so when using hparam search plugins like Optuna, you might want to disable
            # raising the below exception to avoid multirun failure
            raise ex
        finally:
            # always close wandb run (even if exception occurs so multirun won't fail)
            if find_spec("wandb"):  # check if wandb is installed
                import wandb

                if wandb.run:
                    wandb.finish()

        return fn_return

    return wrap


def hydra_dataclass(dataclass: Any) -> Any:
    """Decorator that allows you to use a dataclass as a hydra config by adding it to the hydra
    store.

    Example:
    ```
    @hydra_dataclass
    class MyConfig:
        foo: int = 1
        bar: str = "baz"

    # Name of the config is the snake case version of the (CamelCase) class name
    @hydra.main(config_name="my_config")
    def main(cfg: MyConfig) -> None:
        print(cfg.foo, cfg.bar)
    ```
    """

    dataclass = dataclasses.dataclass(dataclass)

    name = dataclass.__name__
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

    cs = hydra.core.config_store.ConfigStore.instance()
    cs.store(name=name, node=dataclass)

    return dataclass
