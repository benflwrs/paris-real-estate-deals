import os

import pandas as pd
import pytest

from pipeline.dvf_import import parse_dvf_csv

FIXTURE_CSV = os.path.join(os.path.dirname(__file__), "fixtures", "dvf_sample.csv")


@pytest.fixture(scope="module")
def sample_csv_bytes():
    with open(FIXTURE_CSV, "rb") as f:
        return f.read()


def test_parse_filters_to_appartement_maison(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert set(df["property_type"].unique()) <= {"Appartement", "Maison"}


def test_parse_drops_rows_missing_required_fields(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert df["price"].isna().sum() == 0
    assert df["surface_m2"].isna().sum() == 0
    assert df["longitude"].isna().sum() == 0
    assert df["latitude"].isna().sum() == 0


def test_parse_drops_zero_or_negative_price_and_surface(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert (df["price"] > 0).all()
    assert (df["surface_m2"] > 0).all()


def test_parse_output_columns(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    expected_cols = {
        "mutation_date", "price", "surface_m2", "rooms",
        "property_type", "insee_code", "longitude", "latitude",
    }
    assert expected_cols == set(df.columns)


def test_parse_excludes_dependance_and_local_commercial(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert "Dépendance" not in df["property_type"].values
    assert "Local industriel. commercial ou assimilé" not in df["property_type"].values
