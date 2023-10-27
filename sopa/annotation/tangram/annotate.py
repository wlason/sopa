import logging
import math

import numpy as np
import pandas as pd
import scanpy as sc
import tangram as tg
import torch
from anndata import AnnData
from spatialdata import SpatialData

log = logging.getLogger(__name__)


def tangram_annotate(
    sdata: SpatialData, adata_sc: AnnData, cell_type_key: str, bag_size: int = 10_000, **kwargs
):
    ad_sp = sdata.table

    MultiLevelAnnotation(ad_sp, adata_sc, cell_type_key, bag_size, **kwargs).run()


class MultiLevelAnnotation:
    def __init__(
        self,
        ad_sp: AnnData,
        ad_sc: AnnData,
        cell_type_key: str,
        bag_size: int,
        max_obs_reference: int = 10_000,
        clip_percentile: float = 0.95,
    ):
        self.ad_sp = ad_sp
        self.ad_sc = ad_sc
        self.cell_type_key = cell_type_key
        self.bag_size = bag_size
        self.max_obs_reference = max_obs_reference
        self.clip_percentile = clip_percentile

        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        log.info("Using device:", self.device)

    def _level_suffix(self, level: int) -> str:
        return "" if level == 0 else f"_level{level}"

    def level_obs_key(self, level: int) -> str:
        return f"{self.cell_type_key}{self._level_suffix(level)}"

    def probs_key(self, level: int) -> str:
        return f"tangram_pred{self._level_suffix(level)}"

    @property
    def levels(self):
        i = 0
        while self.level_obs_key(i) in self.ad_sc.obs.columns:
            i += 1
        return i

    def split_indices(self, indices: pd.Series, n_splits: int) -> list(np.ndarray):
        indices = indices.values.copy()
        np.random.shuffle(indices)
        return np.array_split(indices, n_splits)

    def init_obsm(self, level: int):
        self.ad_sp.obsm[self.probs_key(level)] = pd.DataFrame(
            0.0, index=self.ad_sp.obs_names, columns=self.ad_sc.obs[level].unique()
        )

    def get_hard_labels(self, df: pd.DataFrame) -> pd.Series:
        df = df.clip(
            df.quantile(1 - self.clip_percentile), df.quantile(self.clip_percentile), axis=1
        )
        df = (df - df.min()) / (df.max() - df.min())
        return df.idxmax(1)

    def pp_adata(self, ad_sp_: AnnData, split: np.ndarray) -> AnnData:
        "Copied and updated from Tangram pp_adatas()"

        ad_sp_split = ad_sp_[split].copy()
        sc.pp.filter_genes(ad_sp_split, min_cells=1)

        # Calculate uniform density prior as 1/number_of_spots
        ad_sp_split.obs["uniform_density"] = (
            np.ones(ad_sp_split.X.shape[0]) / ad_sp_split.X.shape[0]
        )

        # Calculate rna_count_based density prior as % of rna molecule count
        rna_count_per_spot = np.array(ad_sp_split.X.sum(axis=1)).squeeze()
        ad_sp_split.obs["rna_count_based_density"] = rna_count_per_spot / np.sum(rna_count_per_spot)

        return ad_sp_split

    def run(self):
        for level in range(self.levels):
            log.info(f"> Running on level {level}")

            self.init_obsm(level)
            self.ad_sp.obs[self.level_obs_key(level)] = np.nan

            if level == 0:
                self.run_group()
                continue

            previous_key = self.level_obs_key(level - 1)
            obs_key = self.level_obs_key(level)
            self.ad_sp.obs[obs_key] = self.ad_sp.obs[previous_key]
            groups = self.ad_sc.obs.groupby(previous_key)
            for ct in groups.groups.keys():
                group: pd.DataFrame = groups.get_group(ct)
                indices_sp = self.ad_sp.obs_names[self.ad_sp.obs[previous_key] == ct]

                sub_cts = group[obs_key].unique()
                if len(sub_cts) == 1:
                    self.ad_sp.obsm[self.probs_key(level)].loc[indices_sp, sub_cts[0]] = 1
                    continue

                log.info(f"\n[Cell type {ct}]")
                self.run_group(level, indices_sp, group.index)

        log.info("\nFinished running Tangram")

    def run_group(self, level: int = 0, indices_sp=None, indices_sc=None):
        if indices_sp is not None and len(indices_sp) == 0:
            log.warn("No cell annotated in the upper level...")
            return

        indices_sp = self.ad_sp.obs_names if indices_sp is None else indices_sp
        ad_sp_ = self.ad_sp[indices_sp].copy()

        indices_sc = self.ad_sc.obs_names if indices_sc is None else indices_sc
        ad_sc_ = self.ad_sc[indices_sc].copy()

        if ad_sc_.n_obs >= self.max_obs_reference:
            log.info(f"Subsampling reference to {self.max_obs_reference} cells...")
            sc.pp.subsample(ad_sc_, n_obs=self.max_obs_reference)

        log.info(f"(n_obs_spatial={ad_sp_.n_obs}, n_obs_ref={ad_sc_.n_obs})\n")

        if not self.can_run(ad_sp_, ad_sc_):
            log.info("No annotations at this level")
            return

        n_splits = math.ceil(ad_sp_.n_obs / self.bag_size)
        for i, split in enumerate(self.split_indices(indices_sp, n_splits)):
            log.info(f"   > Split {i + 1} / {n_splits}")

            ad_sp_split = self.pp_adata(ad_sp_, split)

            ad_map = tg.map_cells_to_space(
                ad_sc_,
                ad_sp_split,
                device=self.device,
            )

            tg.project_cell_annotations(ad_map, ad_sp_split, annotation=self.level_obs_key(level))

            res = ad_sp_split.obsm["tangram_ct_pred"]
            self.ad_sp.obsm[self.probs_key(level)].loc[split, res.columns] = res

        df_group = self.ad_sp.obsm[self.probs_key(level)].loc[indices_sp]
        self.ad_sp.obs.loc[indices_sp, self.level_obs_key(level)] = self.get_hard_labels(df_group)

    def can_run(self, ad_sp_: AnnData, ad_sc_: AnnData, min_obs: int = 10) -> bool:
        if ad_sp_.n_obs < min_obs:
            log.info(f"Found only {ad_sp_.n_obs} spatial cells.")
            return False

        if ad_sc_.n_obs < min_obs:
            log.info(f"Found only {ad_sc_.n_obs} reference cells.")
            return False

        return True
