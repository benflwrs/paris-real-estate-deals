import pandas as pd
import pytest

from pipeline.scoring.zone_popularity import (
    compute_listing_velocity,
    compute_household_profile,
    build_zone_popularity_report,
)


def _mk_listing(insee, txn, first_days_ago, last_days_ago, active, surface=50, rooms=2, price=400000):
    now = pd.Timestamp("2026-01-01")
    return {
        "insee_code": insee,
        "transaction_type": txn,
        "first_seen_at": now - pd.Timedelta(days=first_days_ago),
        "last_seen_at": now - pd.Timedelta(days=last_days_ago),
        "is_active": active,
        "surface_m2": surface,
        "rooms": rooms,
        "price": price,
        "price_per_m2": price / surface,
    }


def test_velocity_faster_turnover_scores_higher():
    rows = []
    # Zone A: listings delist quickly (5 days) -> high demand
    for _ in range(5):
        rows.append(_mk_listing("75101", "sale", 5, 0, False))
    # Zone B: listings sit for a long time (60 days) -> low demand
    for _ in range(5):
        rows.append(_mk_listing("75120", "sale", 60, 0, False))
    df = pd.DataFrame(rows)

    velocity = compute_listing_velocity(df)
    a_score = velocity[velocity["insee_code"] == "75101"]["velocity_score"].iloc[0]
    b_score = velocity[velocity["insee_code"] == "75120"]["velocity_score"].iloc[0]
    assert a_score > b_score


def test_velocity_separates_buy_and_rent():
    rows = [_mk_listing("75101", "sale", 30, 0, False), _mk_listing("75101", "rent", 5, 0, False)]
    df = pd.DataFrame(rows)
    velocity = compute_listing_velocity(df)
    assert set(velocity["transaction_type"]) == {"sale", "rent"}


def test_velocity_counts_active_and_delisted():
    rows = [_mk_listing("75101", "sale", 5, 0, False) for _ in range(3)]
    rows += [_mk_listing("75101", "sale", 10, 10, True) for _ in range(2)]
    df = pd.DataFrame(rows)
    velocity = compute_listing_velocity(df)
    row = velocity[velocity["insee_code"] == "75101"].iloc[0]
    assert row["delisted_count"] == 3
    assert row["active_count"] == 2


def test_household_profile_identifies_studio_zone():
    rows = [_mk_listing("75101", "rent", 5, 0, False, surface=22, rooms=1) for _ in range(10)]
    df = pd.DataFrame(rows)
    profile = compute_household_profile(df)
    row = profile[profile["insee_code"] == "75101"].iloc[0]
    assert row["studio_share"] == 1.0
    assert row["family_share"] == 0.0


def test_household_profile_identifies_family_zone():
    rows = [_mk_listing("75116", "sale", 5, 0, False, surface=110, rooms=5) for _ in range(10)]
    df = pd.DataFrame(rows)
    profile = compute_household_profile(df)
    row = profile[profile["insee_code"] == "75116"].iloc[0]
    assert row["family_share"] == 1.0
    assert row["studio_share"] == 0.0


def test_household_profile_price_tier_relative_ranking():
    rows = []
    rows += [_mk_listing("75101", "sale", 5, 0, False, price=200000) for _ in range(5)]  # cheap
    rows += [_mk_listing("75102", "sale", 5, 0, False, price=400000) for _ in range(5)]
    rows += [_mk_listing("75103", "sale", 5, 0, False, price=700000) for _ in range(5)]
    rows += [_mk_listing("75104", "sale", 5, 0, False, price=1500000) for _ in range(5)]
    df = pd.DataFrame(rows)
    profile = compute_household_profile(df)
    cheapest_tier = profile[profile["insee_code"] == "75101"]["price_tier"].iloc[0]
    priciest_tier = profile[profile["insee_code"] == "75104"]["price_tier"].iloc[0]
    assert str(cheapest_tier) == "budget"
    assert str(priciest_tier) == "luxury"


def test_build_zone_popularity_report_merges_both_signals():
    rows = [_mk_listing("75101", "sale", 5, 0, False, surface=25, rooms=1) for _ in range(5)]
    df = pd.DataFrame(rows)
    report = build_zone_popularity_report(df)
    assert "velocity_score" in report.columns
    assert "studio_share" in report.columns
    assert len(report) == 1
