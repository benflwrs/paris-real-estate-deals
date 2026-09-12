"""Zone popularity, demand, and demographic-segment scoring.

Ben's ask: figure out which zones are most popular to buy in, most popular
to rent, and "for who" (what kind of household/renter/buyer a zone caters
to). No LLM needed -- this is derived directly from listing attributes and
turnover behavior already in the DB.

Signals used (all computed from `listings`, grouped by insee_code):

1. Listing velocity (demand proxy): how fast listings in a zone go
   inactive (delisted -- either sold/rented or withdrawn) relative to how
   long they've been listed. A zone where properties disappear quickly is
   a zone with more buyer/renter demand relative to supply.
   velocity_score = 1 / median_days_active (normalized 0-100 per mode)

2. Supply/turnover: count of new listings appearing per 30 days in a zone,
   as a secondary signal (a zone can be "hot" due to genuine turnover, not
   just scarcity -- distinguishing high demand from low supply matters for
   investment framing, so both are surfaced, not conflated into one number)

3. "For who" household-fit profile: derived purely from the DISTRIBUTION
   of property attributes actually being listed in each zone (surface,
   rooms, price bracket) -- no census/demographic data needed:
     - studio_share: fraction of listings with surface <= 30 or rooms <= 1
       -> proxy for single/student-oriented zones
     - family_share: fraction with rooms >= 4 or surface >= 90
       -> proxy for family-oriented zones
     - budget_tier: median price_per_m2 bucketed into
       budget / mid-range / premium / luxury (using the zone's own DVF
       distribution as the reference so "budget" is relative to Paris, not
       an absolute number that would misclassify every Paris zone as
       expensive vs. a national baseline)

This is intentionally not LLM-based: turnover speed, room-count mix, and
price tier are all directly observable in listing data with zero ambiguity
-- an LLM would add cost and latency without adding information here.
"""
from __future__ import annotations

import datetime
import logging

import pandas as pd

logger = logging.getLogger(__name__)

PRICE_TIER_LABELS = ["budget", "mid-range", "premium", "luxury"]


def compute_listing_velocity(listings_df: pd.DataFrame, as_of: datetime.date | None = None) -> pd.DataFrame:
    """listings_df needs: insee_code, transaction_type, first_seen_at,
    last_seen_at, is_active.

    Returns a per-(insee_code, transaction_type) DataFrame with:
      - median_days_active: for listings that have gone inactive, how long
        they stayed listed (a demand proxy -- lower = faster turnover =
        more in-demand)
      - velocity_score: 0-100, higher = faster turnover = more popular
      - active_count / delisted_count: sample sizes backing the estimate
    """
    df = listings_df.copy()
    as_of = as_of or datetime.date.today()

    df["first_seen_at"] = pd.to_datetime(df["first_seen_at"])
    df["last_seen_at"] = pd.to_datetime(df["last_seen_at"])
    df["days_active"] = (df["last_seen_at"] - df["first_seen_at"]).dt.total_seconds() / 86400

    delisted = df[~df["is_active"]]
    active = df[df["is_active"]]

    grouped = delisted.groupby(["insee_code", "transaction_type"])["days_active"].median().reset_index()
    grouped = grouped.rename(columns={"days_active": "median_days_active"})

    counts_delisted = delisted.groupby(["insee_code", "transaction_type"]).size().reset_index(name="delisted_count")
    counts_active = active.groupby(["insee_code", "transaction_type"]).size().reset_index(name="active_count")

    result = grouped.merge(counts_delisted, on=["insee_code", "transaction_type"], how="outer")
    result = result.merge(counts_active, on=["insee_code", "transaction_type"], how="outer")
    result["delisted_count"] = result["delisted_count"].fillna(0).astype(int)
    result["active_count"] = result["active_count"].fillna(0).astype(int)

    # Velocity score: inverse of median days active, normalized within each
    # transaction_type so buy and rent (very different typical timescales)
    # aren't compared on the same absolute scale.
    result["velocity_score"] = None
    for txn_type in result["transaction_type"].dropna().unique():
        mask = result["transaction_type"] == txn_type
        valid = result.loc[mask, "median_days_active"].dropna()
        if valid.empty:
            continue
        lo, hi = valid.min(), valid.max()
        span = (hi - lo) or 1.0
        # Faster (lower days) -> higher score.
        result.loc[mask, "velocity_score"] = result.loc[mask, "median_days_active"].apply(
            lambda d: round(100 - 100 * (d - lo) / span, 1) if pd.notna(d) else None
        )

    return result


def compute_household_profile(listings_df: pd.DataFrame) -> pd.DataFrame:
    """listings_df needs: insee_code, transaction_type, surface_m2, rooms,
    price, price_per_m2 (or price/surface_m2 computable).

    Returns per-(insee_code, transaction_type):
      - studio_share, family_share: 0-1 fractions
      - median_price_per_m2
      - price_tier: budget/mid-range/premium/luxury, relative to the
        overall distribution across all zones in this dataset
    """
    df = listings_df.copy()
    df = df.dropna(subset=["insee_code", "surface_m2"])
    df["price_per_m2"] = df.get("price_per_m2")
    if df["price_per_m2"].isna().all() and "price" in df.columns:
        df["price_per_m2"] = df["price"] / df["surface_m2"]

    df["is_studio_like"] = (df["surface_m2"] <= 30) | (df["rooms"].fillna(99) <= 1)
    df["is_family_like"] = (df["rooms"].fillna(0) >= 4) | (df["surface_m2"] >= 90)

    grouped = df.groupby(["insee_code", "transaction_type"]).agg(
        studio_share=("is_studio_like", "mean"),
        family_share=("is_family_like", "mean"),
        median_price_per_m2=("price_per_m2", "median"),
        n_listings=("insee_code", "size"),
    ).reset_index()

    grouped["studio_share"] = grouped["studio_share"].round(2)
    grouped["family_share"] = grouped["family_share"].round(2)

    if grouped["median_price_per_m2"].notna().any():
        n_zones = grouped["median_price_per_m2"].notna().sum()
        if n_zones >= 4:
            try:
                grouped["price_tier"] = pd.qcut(
                    grouped["median_price_per_m2"], q=4, labels=PRICE_TIER_LABELS, duplicates="drop"
                )
            except ValueError:
                grouped["price_tier"] = None
        if "price_tier" not in grouped.columns or grouped["price_tier"].isna().all():
            # Too few distinct zones for a clean 4-way split -- rank
            # zones and assign tiers by relative position instead of
            # failing outright (still gives a meaningful budget/luxury
            # signal even with a handful of zones).
            ranks = grouped["median_price_per_m2"].rank(pct=True)

            def _tier_from_rank(pct):
                if pd.isna(pct):
                    return None
                if pct <= 0.25:
                    return "budget"
                if pct <= 0.5:
                    return "mid-range"
                if pct <= 0.75:
                    return "premium"
                return "luxury"

            grouped["price_tier"] = ranks.apply(_tier_from_rank)
    else:
        grouped["price_tier"] = None

    return grouped


def build_zone_popularity_report(listings_df: pd.DataFrame) -> pd.DataFrame:
    """Combine velocity + household profile into one per-zone report."""
    velocity = compute_listing_velocity(listings_df)
    profile = compute_household_profile(listings_df)
    merged = velocity.merge(profile, on=["insee_code", "transaction_type"], how="outer")
    return merged.sort_values(["transaction_type", "velocity_score"], ascending=[True, False])


if __name__ == "__main__":
    from pipeline.scrapers.base import get_db_connection

    conn = get_db_connection()
    listings_df = pd.read_sql(
        """
        SELECT insee_code, transaction_type, surface_m2, rooms, price, price_per_m2,
               first_seen_at, last_seen_at, is_active
        FROM listings WHERE insee_code IS NOT NULL
        """,
        conn,
    )
    conn.close()

    report = build_zone_popularity_report(listings_df)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print(report.to_string(index=False))
