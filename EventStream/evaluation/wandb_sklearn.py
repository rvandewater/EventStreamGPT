"""Utilities for collecting baseline performance of fine-tuning tasks defined over ESGPT datasets."""

import copy
import itertools
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import omegaconf
from omegaconf import OmegaConf
import numpy as np
import polars as pl
import polars.selectors as cs
from scipy.stats import bernoulli, loguniform, randint, rv_discrete
from sklearn.decomposition import NMF, PCA
from sklearn.feature_selection import SelectKBest, mutual_info_classif
from sklearn.impute import KNNImputer, SimpleImputer
from sklearn.model_selection import RandomizedSearchCV
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.ensemble import RandomForestClassifier

from ..data.dataset_polars import Dataset
from ..utils import task_wrapper
from .FT_task_baseline import load_flat_rep, add_tasks_from, ESDFlatFeatureLoader

pl.enable_string_cache(True)

from abc import ABC, abstractmethod
import dataclasses
import inspect
import warnings

SKLEARN_CONFIG_MODULES = {}

def registered_sklearn_config(dataclass: Any) -> Any:
    """Decorator that allows you to use a dataclass as a hydra config via the `ConfigStore`

    Adds the decorated dataclass as a `Hydra StructuredConfig object`_ to the `Hydra ConfigStore`_.
    The name of the stored config in the ConfigStore is the snake case version of the CamelCase class name.

    .. _Hydra StructuredConfig object: https://hydra.cc/docs/tutorials/structured_config/intro/

    .. _Hydra ConfigStore: https://hydra.cc/docs/tutorials/structured_config/config_store/
    """

    dataclass = dataclasses.dataclass(dataclass)

    name = dataclass.__name__
    cls_name = name[:-len("Config")]

    if cls_name != dataclass().CLS:
        raise ValueError(f"CLS must be {cls_name} for config class named {name}")

    SKLEARN_CONFIG_MODULES[cls_name] = dataclass

    return dataclass

class BaseSklearnModuleConfig(ABC):
    SKLEARN_COMPONENTS = {
        cls.__name__: cls for cls in [
            PCA, NMF, SelectKBest, mutual_info_classif, KNNImputer, SimpleImputer, MinMaxScaler,
            StandardScaler, ESDFlatFeatureLoader, RandomForestClassifier
        ]
    }
    SKIP_PARAMS = ["CLS", "SKLEARN_COMPONENTS", "SKIP_PARAMS"]

    CLS: str = omegaconf.MISSING

    def get_model(self, seed: int | None = None, **additional_kwargs) -> Any:
        cls = self.SKLEARN_COMPONENTS[self.CLS]

        kwargs = {**self.module_kwargs, **additional_kwargs}
        signature = inspect.signature(cls)
        for k in list(kwargs.keys()):
            if k not in signature.parameters:
                warnings.warn(f"Parameter {k} not in signature of {cls.__name__}. Dropping")
                del kwargs[k]
        if 'random_state' in signature.parameters:
            kwargs['random_state'] = seed
        elif 'seed' in signature.parameters:
            kwargs['seed'] = seed

        return self.SKLEARN_COMPONENTS[self.CLS](**kwargs)

    @property
    def module_kwargs(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if k not in self.SKIP_PARAMS}

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return dict()

@registered_sklearn_config
class RandomForestClassifierConfig(BaseSklearnModuleConfig):
    CLS: str = 'RandomForestClassifier'

    n_estimators: int = 100
    criterion: str = 'gini'
    max_depth: int | None = None
    min_samples_split: int = 2
    min_samples_leaf: int = 1
    min_weight_fraction_leaf: float = 0.0
    max_features: str = 'auto'
    max_leaf_nodes: int | None = None
    min_impurity_decrease: float = 0.0
    bootstrap: bool = True
    oob_score: bool = False
    class_weight: str | None = None
    ccp_alpha: float = 0.0
    max_samples: int | None = None

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return {
            "n_estimators": randint(10, 1000),
            "criterion": ["gini", "entropy"],
            "max_depth": [None, randint(2, 32)],
            "min_samples_split": randint(2, 32),
            "min_samples_leaf": randint(1, 32),
            "min_weight_fraction_leaf": loguniform(1e-5, 0.5),
            "max_features": ["auto", "sqrt", "log2"],
            "max_leaf_nodes": [None, randint(2, 32)],
            "min_impurity_decrease": loguniform(1e-5, 1e-3),
            "bootstrap": [True, False],
            "oob_score": [True, False],
            "class_weight": [None, "balanced", "balanced_subsample"],
            "ccp_alpha": loguniform(1e-5, 1e-3),
            "max_samples": [None, randint(2, 32), uniform(0, 1)],
        }

@registered_sklearn_config
class MinMaxScalerConfig(BaseSklearnModuleConfig):
    CLS: str = 'MinMaxScaler'

@registered_sklearn_config
class StandardScalerConfig(BaseSklearnModuleConfig):
    CLS: str = "StandardScaler"

@registered_sklearn_config
class SimpleImputerConfig(BaseSklearnModuleConfig):
    CLS: str = "SimpleImputer"

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return dict(
            strategy=["constant", "mean", "median", "most_frequent"],
            fill_value=[0],
            add_indicator=[True, False],
        )


    strategy: str = "constant"
    fill_value: float = 0
    add_indicator: bool = True

@registered_sklearn_config
class NMFConfig(BaseSklearnModuleConfig):
    CLS: str = "NMF"

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return dict(n_components=randint(2, 32))

    n_components: int = 2

@registered_sklearn_config
class PCAConfig(BaseSklearnModuleConfig):
    CLS: str = "PCA"

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return dict(n_components=randint(2, 32))

    n_components: int = 2

@registered_sklearn_config
class SelectKBestConfig(BaseSklearnModuleConfig):
    CLS: str = "SelectKBest"

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return dict(k=randint(2, 32))

    k: int = 2

@registered_sklearn_config
class KNNImputerConfig(BaseSklearnModuleConfig):
    CLS: str = "KNNImputer"

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return dict(
            n_neighbors=randint(2, 10), weights=["uniform", "distance"], add_indicator=[True, False]
        )

    n_neighbors: int = 5
    weights: str = "uniform"
    add_indicator: bool = True


@registered_sklearn_config
class ESDFlatFeatureLoaderConfig(BaseSklearnModuleConfig):
    CLS: str = "ESDFlatFeatureLoader"

    WINDOW_OPTIONS = [
        "6h",
        "1d",
        "3d",
        "7d",
        "10d",
        "30d",
        "90d",
        "180d",
        "365d",
        "730d",
        "1825d",
        "3650d",
        "FULL",
    ]

    @classmethod
    def default_param_dist(cls) -> dict[str, Any]:
        return {
            "window_sizes": WindowSizeDist(window_options=cls.WINDOW_OPTIONS),
            "feature_inclusion_frequency": loguniform(a=1e-7, b=1e-3),
            "convert_to_mean_var": bernoulli(0.5),
        }

    window_sizes: list[str] | None = None
    feature_inclusion_frequency: float | None = None
    include_only_measurements: list[str] | None = None
    convert_to_mean_var: bool = True

@dataclasses.dataclass
class SklearnConfig:
    PIPELINE_COMPONENTS = ["feature_selector", "scaling", "imputation", "dim_reduce", "model"]

    seed: int = 1

    dataset_dir: str | Path = omegaconf.MISSING
    save_dir: str | Path = omegaconf.MISSING

    train_subset_size: int | float | None = None

    do_overwrite: bool = False

    task_df_name: str | None = omegaconf.MISSING
    finetuning_task_label: str | None = omegaconf.MISSING

    feature_selector: Any = omegaconf.MISSING
    scaling: Any = None
    imputation: Any = None
    dim_reduce: Any = None
    model: Any = omegaconf.MISSING

    def __post_init__(self):
        if isinstance(self.save_dir, str):
            self.save_dir = Path(self.save_dir)
        if isinstance(self.dataset_dir, str):
            self.dataset_dir = Path(self.save_dir)

        match self.train_subset_size:
            case int() as n_subjects if n_subjects > 0:
                pass
            case float() as frac_subjects if 0 < frac_subjects and frac_subjects < 1:
                pass
            case None:
                pass
            case _:
                raise ValueError(
                    "train_subset_size invalid! Must be either None, a positive int, or a float "
                    f"between 0 and 1. Got {self.train_subset_size}."
                )

    def __get_component_model(self, component: str, **kwargs) -> Any:
        if component not in self.PIPELINE_COMPONENTS:
            raise ValueError(f"Unknown component {component}")

        component_val = getattr(self, component)
        match component_val:
            case None:
                return "passthrough"
            case BaseSklearnModuleConfig():
                pass
            case dict() | omegaconf.DictConfig():
                component_val = SKLEARN_CONFIG_MODULES[component_val['CLS']](**component_val)
                setattr(self, component, component_val)
            case _:
                raise ValueError(
                    f"{component} can only be a SKlearnConfig or None (in which case it is omitted). "
                    f"Got {type(component_val)}({component_val})."
                )

        return component_val.get_model(seed=self.seed, **kwargs)

    def get_model(self, dataset: Dataset) -> Any:
        return Pipeline(
            [("feature_selector", self.__get_component_model("feature_selector", ESD=dataset))] + 
            [(n, self.__get_component_model(n)) for n in self.PIPELINE_COMPONENTS[1:]]
        )


def train_sklearn_pipeline(cfg: SklearnConfig):
    print(f"Saving config to {cfg.save_dir / 'config.yaml'}")
    cfg.save_dir.mkdir(exist_ok=True, parents=True)
    OmegaConf.save(cfg, cfg.save_dir / "config.yaml")


    ESD = Dataset.load(cfg.dataset_dir)

    task_dfs = add_tasks_from(ESD.config.save_dir / "task_dfs")
    task_df = task_dfs[cfg.task_df_name]

    # TODO(mmd): Window sizes may violate start_time constraints in task dfs!

    print(f"Loading representations for {', '.join(cfg.feature_selector.window_sizes)}")
    task_df = task_df.select("subject_id", pl.col("end_time").alias("timestamp"), cfg.finetuning_task_label)

    subjects_included = {}

    if cfg.train_subset_size is not None:
        subject_ids = list(ESD.split_subjects['train'])
        prng = np.random.default_rng(cfg.seed)
        match cfg.train_subset_size:
            case int() as n_subjects if n_subjects > 1:
                subject_ids = prng.choice(subject_ids, size=n_subjects, replace=False)
            case float() as frac if 0 < frac < 1:
                subject_ids = prng.choice(
                    subject_ids, size=int(frac*len(subject_ids)), replace=False
                )
            case _:
                raise ValueError(
                    f"train_subset_size must be either `None`, an int > 1, or a float between 0 and 1; "
                    f"got {train_subset_size}"
                )
        subjects_included["train"] = [int(e) for e in subject_ids]
        subjects_included["tuning"] = [int(e) for e in ESD.split_subjects['tuning']]
        subjects_included["held_out"] = [int(e) for e in ESD.split_subjects['held_out']]

        all_subject_ids = list(
            set(subjects_included["train"]) | set(subjects_included["tuning"]) |
            set(subjects_included["held_out"])
        )
        task_df = task_df.filter(pl.col("subject_id").is_in(all_subject_ids))

    with open(cfg.save_dir / "subjects.json", mode='w') as f:
        json.dump(subjects_included, f)

    flat_reps = load_flat_rep(ESD, window_sizes=cfg.feature_selector.window_sizes, join_df=task_df)
    Xs_and_Ys = {}
    for split in ("train", "tuning", "held_out"):
        st = datetime.now()
        print(f"Loading dataset for {split}")
        out_schema = None
        df = flat_reps[split].collect()

        X = df.drop(["subject_id", "timestamp", cfg.finetuning_task_label])
        Y = df[cfg.finetuning_task_label].to_numpy()
        print(
            f"Done with {split} dataset with X of shape {X.shape} "
            f"(elapsed: {datetime.now() - st})"
        )
        Xs_and_Ys[split] = (X, Y)

    print("Initializing model!")
    model = cfg.get_model(dataset=ESD)

    print("Fitting model!")
    model.fit(*Xs_and_Ys["train"])

    print("Evaluating model!")
    eval_metrics = {}
    for split in ("tuning", "held_out"):
        X, Y = Xs_and_Ys[split]
        probs = model.predict_proba(X)
        for metric_n, metric_fn in [
            ("AUROC", roc_auc_score),
            ("AUPRC", average_precision_score),
            ("Accuracy", accuracy_score),
            ("NLL", log_loss),
        ]:
            eval_metrics[f"{split}/{metric_n}"] = metric_fn(Y, probs[:, 1])

    print(f"Saving model to {cfg.save_dir}")
    with open(cfg.save_dir / "model.pkl", mode='wb') as f:
        pickle.dump(model, f)
    with open(cfg.save_dir / "final_metrics.json", mode="w") as f:
        json.dump(eval_metrics, f)

    return model, eval_metrics

@task_wrapper
def wandb_train_sklearn(cfg: SklearnConfig):
    wandb.init()

    model, eval_metrics = train_sklearn_pipeline(cfg)
    wandb.log(eval_metrics)
