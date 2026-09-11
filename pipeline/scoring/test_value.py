import numpy as np
import pandas as pd
import pytest

from pipeline.scoring.value import compute_value_scores, fit_value_model


def _synthetic_dvf(n_per_zone: int = 200, seed: int = 0) -> pd.DataFrame:
    """Two zones with clearly different price/m2 baselines, so a fitted
    model should learn zone effects, not just surface."""
    rng = np.random.default_rng(seed)
    rows = []
    zone_price_per_m2 = {"75116": 12000, "75119": 7000}  # wealthy vs cheaper arrondissement
    for insee_code, ppm2 in zone_price_per_m2.items():
        surfaces = rng.uniform(20, 120, n_per_zone)
        noise = rng.normal(1.0, 0.05, n_per_zone)
        prices = surfaces * ppm2 * noise
        rooms = np.clip((surfaces / 25).round(), 1, 6)
        rows.append(
            pd.DataFrame(
                {
                    "surface_m2": surfaces,
                    "rooms": rooms,
                    "insee_code": insee_code,
                    "price": prices,
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


@pytest.fixture(scope="module")
def fitted_model():
    dvf_df = _synthetic_dvf()
    return fit_value_model(dvf_df)


def test_fit_value_model_produces_reasonable_predictions(fitted_model):
    listings = pd.DataFrame(
        {"surface_m2": [50], "rooms": [2], "insee_code": ["75116"], "price": [600000]}
    )
    predicted = fitted_model.predict_expected_price(listings)
    # True fair price ~50*12000=600000; model should be in a sane ballpark.
    assert 400000 < predicted.iloc[0] < 800000


def test_underpriced_listing_gets_high_value_score(fitted_model):
    # Fair price for 50m2 in 75116 is ~600k; list it at 400k (a steal).
    listings = pd.DataFrame(
        {"surface_m2": [50], "rooms": [2], "insee_code": ["75116"], "price": [400000]}
    )
    scores = compute_value_scores(listings, fitted_model)
    assert scores.iloc[0] > 60  # above the fairly-priced midpoint of 50


def test_overpriced_listing_gets_low_value_score(fitted_model):
    listings = pd.DataFrame(
        {"surface_m2": [50], "rooms": [2], "insee_code": ["75116"], "price": [900000]}
    )
    scores = compute_value_scores(listings, fitted_model)
    assert scores.iloc[0] < 40


def test_fairly_priced_listing_scores_near_midpoint(fitted_model):
    listings = pd.DataFrame(
        {"surface_m2": [50], "rooms": [2], "insee_code": ["75116"], "price": [600000]}
    )
    scores = compute_value_scores(listings, fitted_model)
    assert 35 < scores.iloc[0] < 65


def test_cheaper_zone_has_lower_expected_price_for_same_surface(fitted_model):
    listings = pd.DataFrame(
        {
            "surface_m2": [50, 50],
            "rooms": [2, 2],
            "insee_code": ["75116", "75119"],
            "price": [600000, 600000],
        }
    )
    predicted = fitted_model.predict_expected_price(listings)
    assert predicted.iloc[0] > predicted.iloc[1]


def test_unseen_zone_falls_back_gracefully(fitted_model):
    listings = pd.DataFrame(
        {"surface_m2": [50], "rooms": [2], "insee_code": ["99999"], "price": [500000]}
    )
    predicted = fitted_model.predict_expected_price(listings)
    assert predicted.iloc[0] > 0  # doesn't crash, produces some baseline prediction
