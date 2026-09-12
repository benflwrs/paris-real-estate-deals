"""How much does each property characteristic move the price?

Ben's ask: quantify the price impact of floor level, elevator (or lack of),
balcony, etc. -- e.g. "how much does being on the 7th floor with no
elevator impact price" and "how much does a balcony improve price".

DVF (the government sold-transaction data) does NOT record floor,
elevator, balcony, or furnished status -- only price/surface/rooms/zone.
So this can't be answered from DVF alone. Instead we run a hedonic
regression directly on the SCRAPED LIVE LISTINGS (which do carry these
fields), using the DVF-implied zone price/m2 as a control variable so the
model isolates the effect of each amenity from the underlying "which
neighborhood is this in" effect rather than conflating the two.

Model: log(price_per_m2) ~ log(zone_baseline_price_per_m2) + floor +
       is_ground_floor + has_elevator + floor_without_elevator (interaction)
       + has_balcony + has_parking + in_residence + is_furnished +
       building_age + dpe_ordinal

Coefficients on a log-price-per-m2 target are approximately percentage
effects (coef * 100 = % change in price/m2 for a one-unit change in that
feature, holding others constant).

Caveat (be upfront about this with Ben): this is fit on however many
active listings the DB has with complete-enough fields at a given time,
which starts small (Bien'ici only, ~300 listings and growing as scraping
continues). Coefficients on lower-frequency features (e.g. is_furnished --
mostly a rental attribute, rare in sale listings) will be noisy /
low-confidence with a small n; re-run this periodically as more data
accumulates and trust the wide-sample coefficients (floor, elevator,
balcony) more than the sparse ones early on.
"""
from __future__ import annotations

import datetime
import logging

import numpy as np
import pandas as pd
import statsmodels.api as sm

logger = logging.getLogger(__name__)

DPE_ORDER = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7}

FEATURE_COLUMNS = [
    "log_zone_baseline_ppm2",
    "floor",
    "is_ground_floor",
    "has_elevator",
    "floor_without_elevator",
    "has_balcony",
    "has_parking",
    "in_residence",
    "is_furnished",
    "building_age",
    "dpe_ordinal",
]


def build_feature_frame(listings_df: pd.DataFrame, zone_baseline_ppm2: pd.Series) -> pd.DataFrame:
    """listings_df: raw listings table columns (price, surface_m2, floor,
    has_elevator, has_balcony, has_parking, in_residence, is_furnished,
    construction_year, dpe_class, insee_code).
    zone_baseline_ppm2: Series indexed by insee_code -> DVF median price/m2.
    """
    df = listings_df.copy()
    df = df.dropna(subset=["price", "surface_m2", "insee_code"])
    df = df[df["surface_m2"] > 0]
    df["price_per_m2"] = df["price"] / df["surface_m2"]
    df = df[df["price_per_m2"] > 0]

    df["zone_baseline_ppm2"] = df["insee_code"].map(zone_baseline_ppm2)
    df = df.dropna(subset=["zone_baseline_ppm2"])
    df = df[df["zone_baseline_ppm2"] > 0]

    df["log_price_per_m2"] = np.log(df["price_per_m2"])
    df["log_zone_baseline_ppm2"] = np.log(df["zone_baseline_ppm2"])

    df["floor"] = df["floor"].fillna(df["floor"].median() if df["floor"].notna().any() else 0)
    df["is_ground_floor"] = (df["floor"] == 0).astype(int)
    df["has_elevator"] = df["has_elevator"].fillna(False).astype(int)
    df["floor_without_elevator"] = df["floor"] * (1 - df["has_elevator"])
    df["has_balcony"] = df["has_balcony"].fillna(False).astype(int)
    df["has_parking"] = df["has_parking"].fillna(False).astype(int)
    df["in_residence"] = df["in_residence"].fillna(False).astype(int)
    df["is_furnished"] = df["is_furnished"].fillna(False).astype(int)

    current_year = datetime.date.today().year
    df["building_age"] = current_year - df["construction_year"]
    df["building_age"] = df["building_age"].fillna(df["building_age"].median() if df["building_age"].notna().any() else 50)

    df["dpe_ordinal"] = df["dpe_class"].map(DPE_ORDER)
    df["dpe_ordinal"] = df["dpe_ordinal"].fillna(df["dpe_ordinal"].median() if df["dpe_ordinal"].notna().any() else 4)

    return df


def fit_feature_impact_model(feature_df: pd.DataFrame):
    """Fit OLS and return the fitted statsmodels result object.

    Drops any feature column with zero variance (e.g. is_furnished when
    every listing in the current sample lacks that field) before fitting --
    a constant column is collinear with the intercept and makes the design
    matrix rank-deficient, producing unstable/ambiguous coefficients.
    """
    usable_columns = [c for c in FEATURE_COLUMNS if feature_df[c].astype(float).nunique() > 1]
    X = feature_df[usable_columns].astype(float)
    X = sm.add_constant(X)
    y = feature_df["log_price_per_m2"].astype(float)
    model = sm.OLS(y, X, missing="drop")
    return model.fit()


def summarize_impacts(result) -> list[dict]:
    """Turn OLS coefficients into human-readable % price impacts, with
    p-values so low-confidence estimates are visibly flagged."""
    rows = []
    for name in FEATURE_COLUMNS:
        if name not in result.params.index:
            continue
        coef = result.params[name]
        pvalue = result.pvalues[name]
        pct_impact = (np.exp(coef) - 1) * 100  # for binary/near-binary features
        rows.append(
            {
                "feature": name,
                "coefficient": round(float(coef), 4),
                "approx_pct_impact": round(float(pct_impact), 2),
                "p_value": round(float(pvalue), 4),
                "significant_at_5pct": bool(pvalue < 0.05),
            }
        )
    return rows


if __name__ == "__main__":
    import json

    from pipeline.scrapers.base import get_db_connection

    conn = get_db_connection()
    listings_df = pd.read_sql(
        """
        SELECT price, surface_m2, floor, has_elevator, has_balcony, has_parking,
               in_residence, is_furnished, construction_year, dpe_class, insee_code
        FROM listings WHERE is_active = true AND price IS NOT NULL AND surface_m2 IS NOT NULL
        """,
        conn,
    )
    zone_baseline = pd.read_sql(
        """
        SELECT insee_code, percentile_cont(0.5) WITHIN GROUP (ORDER BY price/surface_m2) as median_ppm2
        FROM dvf_transactions GROUP BY insee_code
        """,
        conn,
    ).set_index("insee_code")["median_ppm2"]
    conn.close()

    feature_df = build_feature_frame(listings_df, zone_baseline)
    print(f"fitting on {len(feature_df)} listings")
    result = fit_feature_impact_model(feature_df)
    print(result.summary())
    print(json.dumps(summarize_impacts(result), indent=2))
