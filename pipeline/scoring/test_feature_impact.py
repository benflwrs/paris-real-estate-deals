import numpy as np
import pandas as pd
import pytest

from pipeline.scoring.feature_impact import build_feature_frame, fit_feature_impact_model, summarize_impacts


def _synthetic_listings(n=800, seed=1):
    """Generate listings with KNOWN ground-truth effects baked in, so we
    can verify the regression recovers them (within reasonable tolerance)."""
    rng = np.random.default_rng(seed)
    insee_codes = np.array(["75101", "75116", "75120"])
    zone_ppm2 = {"75101": 11000, "75116": 11500, "75120": 8000}

    insee = rng.choice(insee_codes, n)
    surface = rng.uniform(20, 120, n)
    floor = rng.integers(0, 8, n)
    has_elevator = rng.integers(0, 2, n).astype(bool)
    has_balcony = rng.integers(0, 2, n).astype(bool)
    has_parking = rng.integers(0, 2, n).astype(bool)
    construction_year = rng.integers(1900, 2020, n)

    base_ppm2 = np.array([zone_ppm2[c] for c in insee])

    # Ground truth effects (log scale):
    # - each floor without elevator: -2% per floor
    # - ground floor: -8%
    # - balcony: +5%
    # - parking: +3%
    log_ppm2 = np.log(base_ppm2)
    log_ppm2 += np.where(has_elevator, 0, -0.02 * floor)
    log_ppm2 += np.where(floor == 0, -0.08, 0)
    log_ppm2 += np.where(has_balcony, 0.05, 0)
    log_ppm2 += np.where(has_parking, 0.03, 0)
    log_ppm2 += rng.normal(0, 0.03, n)  # small noise

    ppm2 = np.exp(log_ppm2)
    price = ppm2 * surface

    return pd.DataFrame(
        {
            "price": price,
            "surface_m2": surface,
            "floor": floor,
            "has_elevator": has_elevator,
            "has_balcony": has_balcony,
            "has_parking": has_parking,
            "in_residence": rng.integers(0, 2, n).astype(bool),
            "is_furnished": np.array([None] * n),
            "construction_year": construction_year,
            "dpe_class": rng.choice(["B", "C", "D", "E"], n),
            "insee_code": insee,
        }
    ), pd.Series(zone_ppm2)


def test_build_feature_frame_produces_expected_columns():
    listings_df, zone_baseline = _synthetic_listings()
    feature_df = build_feature_frame(listings_df, zone_baseline)
    assert "log_price_per_m2" in feature_df.columns
    assert "floor_without_elevator" in feature_df.columns
    assert len(feature_df) > 0


def test_model_recovers_ground_floor_penalty():
    listings_df, zone_baseline = _synthetic_listings()
    feature_df = build_feature_frame(listings_df, zone_baseline)
    result = fit_feature_impact_model(feature_df)
    # ground truth: -8% (log coef ~ -0.08)
    coef = result.params["is_ground_floor"]
    assert -0.14 < coef < -0.02


def test_model_recovers_floor_without_elevator_penalty():
    listings_df, zone_baseline = _synthetic_listings()
    feature_df = build_feature_frame(listings_df, zone_baseline)
    result = fit_feature_impact_model(feature_df)
    # ground truth: -2% per floor without elevator
    coef = result.params["floor_without_elevator"]
    assert -0.04 < coef < 0.0


def test_model_recovers_balcony_premium():
    listings_df, zone_baseline = _synthetic_listings()
    feature_df = build_feature_frame(listings_df, zone_baseline)
    result = fit_feature_impact_model(feature_df)
    # ground truth: +5%
    coef = result.params["has_balcony"]
    assert 0.0 < coef < 0.10


def test_model_recovers_parking_premium():
    listings_df, zone_baseline = _synthetic_listings()
    feature_df = build_feature_frame(listings_df, zone_baseline)
    result = fit_feature_impact_model(feature_df)
    assert 0.0 < result.params["has_parking"] < 0.08


def test_summarize_impacts_returns_expected_shape():
    listings_df, zone_baseline = _synthetic_listings()
    feature_df = build_feature_frame(listings_df, zone_baseline)
    result = fit_feature_impact_model(feature_df)
    summary = summarize_impacts(result)
    features = {row["feature"] for row in summary}
    assert "has_balcony" in features
    assert "is_ground_floor" in features
    for row in summary:
        assert "approx_pct_impact" in row
        assert "significant_at_5pct" in row


def test_zone_baseline_coefficient_near_one():
    # log_zone_baseline_ppm2 should have a coefficient near 1.0 since
    # price_per_m2 is constructed to scale ~linearly with the zone baseline.
    listings_df, zone_baseline = _synthetic_listings()
    feature_df = build_feature_frame(listings_df, zone_baseline)
    result = fit_feature_impact_model(feature_df)
    assert 0.8 < result.params["log_zone_baseline_ppm2"] < 1.2
