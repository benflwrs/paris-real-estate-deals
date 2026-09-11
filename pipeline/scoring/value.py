"""Value / cost-effectiveness scoring.

For each active listing, compare its price against a hedonic regression
fitted on DVF (comparable sold transactions in the same zone), producing a
`value_score` where higher = better deal (listing priced below what
similar properties actually sold for).

This deliberately uses a simple tabular regression (scikit-learn), not an
LLM or deep model -- price/surface/floor/rooms/zone is a small, well
understood feature set where a GradientBoostingRegressor or even linear
regression with zone fixed effects works well and stays interpretable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ValueModel:
    """Thin wrapper around a fitted sklearn regressor + the zone encoding
    it expects, so scoring code doesn't need to know sklearn internals."""

    model: object
    feature_columns: list[str]
    zone_column: str = "insee_code"

    def predict_expected_price(self, features: pd.DataFrame) -> pd.Series:
        X = pd.get_dummies(features[self.feature_columns], columns=[self.zone_column])
        # Align columns to what the model was trained on; missing dummy
        # zones (unseen at fit time) default to 0 across all zone columns.
        X = X.reindex(columns=self.model.feature_names_in_, fill_value=0)
        return pd.Series(self.model.predict(X), index=features.index)


def fit_value_model(dvf_df: pd.DataFrame) -> ValueModel:
    """Fit a hedonic price model on DVF transactions.

    Expected columns: price, surface_m2, rooms, insee_code.
    (construction_year / floor are DVF-absent, so the DVF-fitted baseline
    only captures surface+rooms+zone; per-listing floor/construction
    effects can be added as a secondary adjustment once enough live
    listing history accumulates to fit them empirically.)
    """
    from sklearn.ensemble import GradientBoostingRegressor

    feature_columns = ["surface_m2", "rooms", "insee_code"]
    X = pd.get_dummies(dvf_df[feature_columns], columns=["insee_code"])
    y = dvf_df["price"]

    model = GradientBoostingRegressor(n_estimators=200, max_depth=3, random_state=42)
    model.fit(X, y)
    model.feature_names_in_ = X.columns  # sklearn sets this automatically on fit

    return ValueModel(model=model, feature_columns=feature_columns)


def compute_value_scores(listings_df: pd.DataFrame, value_model: ValueModel) -> pd.Series:
    """Return a 0-100 value_score per listing: higher = bigger discount vs
    the model's expected price for a comparable property in that zone."""
    expected_price = value_model.predict_expected_price(listings_df)
    discount_ratio = (expected_price - listings_df["price"]) / expected_price

    # Clip to a sane range then rescale to 0-100 (50 = fairly priced).
    clipped = discount_ratio.clip(-0.5, 0.5)
    return ((clipped + 0.5) * 100).round(1)
